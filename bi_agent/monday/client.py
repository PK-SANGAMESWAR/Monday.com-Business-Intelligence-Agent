"""GraphQL transport: authentication, timeouts, retry, failure classification.

This module knows about HTTP and errors. It does not know what a board is, let
alone what a deal is — `boards.py` owns the former and F04 owns the latter.

The design problem it solves is that monday.com reports failure in three
incompatible ways, and only one of them looks like a failure:

1. an HTTP status code, as expected;
2. **HTTP 200 carrying an ``errors[]`` array** — the awkward one, because a client
   that classifies on status alone treats a rejected token as success and hands
   ``None`` upward, where it surfaces three layers away as an unrelated crash;
3. a top-level ``error_message`` / ``error_code`` pair on some 4xx responses.

:func:`classify_failure` therefore keys on status *and* on message content, and
every path lands on one of F01's typed errors — which is what makes FR-16
testable, because "degrades gracefully" becomes an assertion about which
exception class was raised and whether a retry happened.

**On not using `tenacity`** (plan section 5 listed it provisionally): the retry
predicate here is "retry exactly the two error classes our own classifier
produced", which is a two-line condition. Tenacity's value is decorator
ergonomics and policy composition we would not use. The explicit loop below is
about 30 lines, and because the sleep is injected, the backoff tests assert on
scheduling decisions in microseconds instead of actually waiting 7 seconds.
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
    MondayAuthError,
    MondayError,
    MondayQueryError,
    MondayRateLimitError,
    MondayUnavailableError,
    ReadOnlyViolationError,
)
from bi_agent.monday.queries import QueryDocument, verify_read_only

__all__ = ["MondayClient", "classify_failure"]

logger = logging.getLogger(__name__)

#: How much of a failing response body reaches the log. Enough to diagnose, not
#: enough to dump a whole board into a log file.
BODY_LOG_LIMIT = 500

#: Substrings that mean "your credential was refused", matched case-folded
#: against every error message in the payload. Content matching is needed
#: because this can arrive as an HTTP 200.
_AUTH_MARKERS = (
    "not authenticated",
    "unauthenticated",
    "unauthorized",
    "authentication",
    "invalid token",
    "forbidden",
    "permission",
)

#: Substrings that mean "slow down". monday.com meters GraphQL by complexity
#: points rather than request count, and reports exhaustion as a 200.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "ratelimit",
    "too many requests",
    "complexity",
    "budget exhausted",
    "throttle",
    "depth limit",
)


def _messages_in(body: Any) -> list[str]:
    """Every error message in a response body, across all three shapes."""
    if not isinstance(body, dict):
        return []

    messages: list[str] = []
    errors = body.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, dict):
                message = error.get("message")
                if message:
                    messages.append(str(message))
            elif isinstance(error, str):
                messages.append(error)

    for key in ("error_message", "error_code", "errorMessage"):
        value = body.get(key)
        if isinstance(value, str) and value:
            messages.append(value)

    return messages


def _matches(messages: list[str], markers: tuple[str, ...]) -> bool:
    folded = " ".join(messages).casefold()
    return any(marker in folded for marker in markers)


def _seconds_from_reset_hint(messages: list[str]) -> float | None:
    """Pull ``reset in 37 seconds`` out of a complexity message.

    On the complexity path there is no ``Retry-After`` header, so this sentence
    is the only wait hint the API gives us. Guessing instead would mean either
    retrying too early (and being refused again) or waiting far too long.
    """
    for message in messages:
        folded = message.casefold()
        marker = "reset in "
        start = folded.find(marker)
        if start == -1:
            continue
        digits = ""
        for char in folded[start + len(marker) :]:
            if char.isdigit():
                digits += char
            elif digits:
                break
        if digits:
            return float(digits)
    return None


def _retry_after_from(response: httpx.Response, messages: list[str]) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header.strip())
        except ValueError:
            # An HTTP-date form is legal but monday.com does not use it; falling
            # through to our own backoff is safer than mis-parsing a date.
            logger.debug("unparsable Retry-After header: %r", header)
    return _seconds_from_reset_hint(messages)


def classify_failure(
    response: httpx.Response, body: Any, *, document_name: str
) -> MondayError | None:
    """Map a response to a typed error, or ``None`` when it is a success.

    Deliberately a pure function of ``(status, body)``: the classification table
    in the feature doc is the part most likely to need correcting against the
    live API, and keeping it free of transport state means a correction touches
    nothing else.
    """
    messages = _messages_in(body)
    detail = "; ".join(messages) if messages else response.reason_phrase or "no detail"
    status = response.status_code

    if status in (401, 403) or (messages and _matches(messages, _AUTH_MARKERS)):
        return MondayAuthError(
            f"monday.com refused the credential on {document_name} "
            f"(HTTP {status}): {detail}"
        )

    if status == 429 or (messages and _matches(messages, _RATE_LIMIT_MARKERS)):
        return MondayRateLimitError(
            f"monday.com is throttling {document_name} (HTTP {status}): {detail}",
            retry_after=_retry_after_from(response, messages),
        )

    if status >= 500:
        return MondayUnavailableError(
            f"monday.com returned HTTP {status} for {document_name}: {detail}"
        )

    if messages:
        return MondayQueryError(
            f"monday.com rejected {document_name} (HTTP {status}): {detail}"
        )

    if status != 200:
        return MondayQueryError(
            f"monday.com returned an unexpected HTTP {status} for {document_name}: "
            f"{detail}"
        )

    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        # No `errors[]`, HTTP 200, and still nothing usable. Raising here rather
        # than returning None is the difference between a named error and a
        # KeyError four layers up.
        return MondayQueryError(
            f"monday.com returned no usable data for {document_name}: "
            f"{str(body)[:BODY_LOG_LIMIT]}"
        )

    return None


class MondayClient:
    """An authenticated, read-only GraphQL client for monday.com.

    Read-only is structural, not advisory: :meth:`execute` accepts only a
    :class:`~bi_agent.monday.queries.QueryDocument`, and every instance of that
    type is built in `queries.py` and verified at import time. A caller cannot
    express a write, because there is no parameter that would carry one.
    """

    #: Longest single backoff wait. Unbounded doubling would eventually park a
    #: user behind a multi-minute silence for no benefit.
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
        """The one place the token is read, and it goes straight into a header.

        `API-Version` is pinned from settings so a server-side default change
        cannot silently alter response shapes underneath our fixtures.
        """
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
        """Release the connection pool. Idempotent.

        Streamlit reruns the whole script on every interaction, so a client that
        leaks its transport leaks one per user click.
        """
        if self._owns_client and not self._http.is_closed:
            self._http.close()

    def __enter__(self) -> MondayClient:
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
        document: QueryDocument,
        variables: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a registry document and return ``response["data"]``.

        Raises :class:`ReadOnlyViolationError` for anything that is not a
        verified :class:`QueryDocument` — including a plain string that happens
        to contain a valid read query. The type *is* the permission.
        """
        if not isinstance(document, QueryDocument):
            raise ReadOnlyViolationError(
                "execute() accepts only a verified QueryDocument from "
                "bi_agent.monday.queries, not "
                f"{type(document).__name__}. Nothing was sent."
            )

        # The gate already ran at import time. Re-running it costs microseconds
        # and closes the gap where a document is built at runtime by mistake.
        verify_read_only(document.text, name=f"document {document.name!r}")

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
        document: QueryDocument,
        variables: dict[str, Any] | None,
        rid: str,
        attempt: int,
    ) -> dict[str, Any]:
        payload = {"query": document.text, "variables": variables or {}}
        started = time.perf_counter()

        logger.debug(
            "POST %s document=%s attempt=%d variables=%s",
            self.settings.monday_api_url,
            document.name,
            attempt + 1,
            sorted((variables or {}).keys()),
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
            # Deliberately not interpolating `exc` verbatim into the message
            # beyond its type and text: httpx puts the request URL in some repr
            # forms, and the URL is the one place a credential could appear.
            raise MondayUnavailableError(
                f"{document.name} could not reach monday.com: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        body = self._decode(response, document, rid)
        failure = classify_failure(response, body, document_name=document.name)

        if failure is not None:
            # The body goes to the log, not to the exception message, and the log
            # is where F01's redaction filter is installed — monday.com echoes
            # request context in some error payloads, so this is the leak path
            # that filter was written for.
            logger.error(
                "%s -> HTTP %d in %.0fms; body: %s",
                document.name,
                response.status_code,
                elapsed_ms,
                str(body)[:BODY_LOG_LIMIT],
                extra={"request_id": rid},
            )
            raise failure

        data: dict[str, Any] = body["data"]
        self._log_success(document, data, elapsed_ms, rid)
        return data

    def _decode(
        self, response: httpx.Response, document: QueryDocument, rid: str
    ) -> Any:
        """Parse the body, tolerating one that is not JSON at all.

        A reverse proxy in front of the API will happily return an HTML error
        page with a 200, and a JSON traceback is not an acceptable answer to a
        founder's question.
        """
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "%s -> HTTP %d with a body that is not JSON: %s",
                document.name,
                response.status_code,
                response.text[:BODY_LOG_LIMIT],
                extra={"request_id": rid},
            )
            raise MondayQueryError(
                f"monday.com returned a body that is not JSON for {document.name} "
                f"(HTTP {response.status_code})"
            ) from exc

    def _log_success(
        self,
        document: QueryDocument,
        data: dict[str, Any],
        elapsed_ms: float,
        rid: str,
    ) -> None:
        """One DEBUG line per request, including complexity spend (NFR-7)."""
        complexity = data.get("complexity")
        if isinstance(complexity, dict):
            logger.debug(
                "%s ok in %.0fms; complexity spent=%s before=%s after=%s",
                document.name,
                elapsed_ms,
                complexity.get("query"),
                complexity.get("before"),
                complexity.get("after"),
                extra={"request_id": rid},
            )
        else:
            logger.debug(
                "%s ok in %.0fms",
                document.name,
                elapsed_ms,
                extra={"request_id": rid},
            )

    def _delay_for(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with full jitter, or the API's own instruction.

        Full jitter — a draw from ``[0, bound]`` rather than the bound itself —
        matters because without it every client retrying a recovering server
        retries in lockstep and knocks it back over.
        """
        if retry_after is not None and retry_after > 0:
            return float(retry_after)
        bound = min(2.0**attempt, self.BACKOFF_CAP_SECONDS)
        return bound * self._jitter()
