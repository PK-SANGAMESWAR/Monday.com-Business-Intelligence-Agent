"""Write transport: auth, retry, throttle, progress.

`SeedWriter` is the mirror image of `bi_agent.monday.client.MondayClient`
(F03 section 3.2): same auth header construction, same retry/backoff shape,
same reliance on `classify_failure` for turning a response into a typed
error. The ~40 lines of duplicated retry loop are paid deliberately so the
write capability lives in exactly one directory `bi_agent/` never imports —
see the module docstring in `scripts/seeding/mutations.py`.

`classify_failure` itself is *not* duplicated: it is a pure function of
`(status, body)` with no opinion about reads versus writes, so it is imported
straight from F02.

`Pacer` is the items-per-minute throttle (F03 section 3.7). It sleeps only
when a write would otherwise land sooner than the configured interval, and
both the clock and the sleep function are injected so tests assert on
scheduling decisions rather than on wall-clock time.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any
from uuid import uuid4

import httpx

from bi_agent.config import Settings, get_settings
from bi_agent.errors import (
    MondayQueryError,
    MondayRateLimitError,
    MondayUnavailableError,
)
from bi_agent.monday.client import classify_failure
from scripts.seeding.errors import WriteGateError
from scripts.seeding.mutations import MutationDocument

__all__ = ["Pacer", "SeedWriter"]

logger = logging.getLogger(__name__)

#: How much of a failing response body reaches the log. Matches F02.
BODY_LOG_LIMIT = 500


class Pacer:
    """Sleeps as needed to hold a target items-per-minute rate.

    `items_per_minute=None` (or `<= 0`) disables pacing entirely: `wait()`
    becomes a no-op, which is what the calibration probe and the tiny
    respx-mocked integration tests want.
    """

    def __init__(
        self,
        items_per_minute: float | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 60.0 / items_per_minute if items_per_minute else 0.0
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_call: float | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def wait(self) -> None:
        """Block until at least one interval has passed since the last call.

        Calls `monotonic()` exactly once per invocation, so a test can drive
        the clock with one value per `wait()` call rather than reasoning
        about how many times the implementation happens to read it.
        """
        if self._interval <= 0:
            return
        now = self._monotonic()
        if self._last_call is not None:
            deficit = self._interval - (now - self._last_call)
            if deficit > 0:
                self._sleep(deficit)
        self._last_call = now


class SeedWriter:
    """An authenticated GraphQL client that sends only reviewed writes.

    `execute()` accepts only a :class:`~scripts.seeding.mutations.MutationDocument`
    — a read `QueryDocument` from `bi_agent.monday.queries`, or a bare string,
    is refused before any HTTP call (`test_write_gate.py` cases 53-55).
    """

    #: Longest single backoff wait. Matches `MondayClient.BACKOFF_CAP_SECONDS`.
    BACKOFF_CAP_SECONDS = 30.0

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings or get_settings()
        self._sleep = sleep
        self._jitter = jitter
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(self.settings.http_timeout_seconds),
            headers=self._headers(),
        )

    # --- construction helpers ---

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.settings.monday_api_key.get_secret_value(),
            "API-Version": self.settings.monday_api_version,
            "Content-Type": "application/json",
        }

    # --- lifecycle ---

    @property
    def is_closed(self) -> bool:
        return self._http.is_closed

    def close(self) -> None:
        if self._owns_client and not self._http.is_closed:
            self._http.close()

    def __enter__(self) -> SeedWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # --- the request ---

    def execute(
        self,
        document: MutationDocument,
        variables: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(document, MutationDocument):
            raise WriteGateError(
                "SeedWriter.execute() accepts only a verified MutationDocument "
                "from scripts.seeding.mutations, not "
                f"{type(document).__name__}. Nothing was sent."
            )

        rid = request_id or uuid4().hex[:8]
        attempt = 0

        while True:
            try:
                return self._attempt(document, variables, rid, attempt)
            except (MondayRateLimitError, MondayUnavailableError) as exc:
                if attempt >= self.settings.max_retries:
                    logger.error(
                        "%s failed after %d attempt(s): %s",
                        document.name,
                        attempt + 1,
                        exc,
                        extra={"request_id": rid},
                    )
                    raise

                delay = self._delay_for(attempt, getattr(exc, "retry_after", None))
                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %.2fs: %s",
                    document.name,
                    attempt + 1,
                    self.settings.max_retries + 1,
                    delay,
                    exc,
                    extra={"request_id": rid},
                )
                self._sleep(delay)
                attempt += 1

    def _attempt(
        self,
        document: MutationDocument,
        variables: dict[str, Any] | None,
        rid: str,
        attempt: int,
    ) -> dict[str, Any]:
        payload = {"query": document.text, "variables": variables or {}}

        logger.debug(
            "POST %s document=%s attempt=%d",
            self.settings.monday_api_url,
            document.name,
            attempt + 1,
            extra={"request_id": rid},
        )

        try:
            response = self._http.post(
                self.settings.monday_api_url,
                json=payload,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise MondayUnavailableError(
                f"{document.name} timed out after "
                f"{self.settings.http_timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise MondayUnavailableError(
                f"{document.name} could not reach monday.com: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # See bi_agent.monday.client.MondayClient._attempt: a non-2xx body is
        # decoded tolerantly so a non-JSON throttle/gateway page still maps to
        # a retryable error via the status code, not a permanent one.
        body = self._decode(response, document, rid, strict=response.status_code == 200)
        failure = classify_failure(response, body, document_name=document.name)

        if failure is not None:
            logger.error(
                "%s -> HTTP %d; body: %s",
                document.name,
                response.status_code,
                str(body)[:BODY_LOG_LIMIT],
                extra={"request_id": rid},
            )
            raise failure

        data: dict[str, Any] = body["data"]
        return data

    def _decode(
        self,
        response: httpx.Response,
        document: MutationDocument,
        rid: str,
        *,
        strict: bool = True,
    ) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "%s -> HTTP %d with a body that is not JSON",
                document.name,
                response.status_code,
                extra={"request_id": rid},
            )
            if not strict:
                return None
            raise MondayQueryError(
                f"monday.com returned a body that is not JSON for "
                f"{document.name} (HTTP {response.status_code})"
            ) from exc

    def _delay_for(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None and retry_after > 0:
            return float(retry_after)
        bound = min(2.0**attempt, self.BACKOFF_CAP_SECONDS)
        return bound * self._jitter()


def encode_column_values(values: dict[str, Any]) -> str:
    """`column_values` as the JSON string monday.com's `JSON` scalar expects."""
    return json.dumps(values)
