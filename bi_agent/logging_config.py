"""Structured logging that cannot leak a secret.

`SecretRedactionFilter` is the component that matters. F02 will log request
headers and error bodies on failure paths, and monday.com echoes request context
in some error payloads — so the one time a token leaks will be in an exception
path nobody rehearsed. A filter is unconditional; "just be careful" is not.

It is defence in depth *behind* ``SecretStr``, not a replacement for it:
``SecretStr`` prevents accidental interpolation, the filter catches deliberate-
but-careless interpolation of ``.get_secret_value()``.

Cost, stated honestly: a substring scan per log record. Irrelevant at this
volume — a few hundred records per session.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import IO, Any

from pydantic import SecretStr

__all__ = [
    "DEFAULT_LOG_FORMAT",
    "REDACTION_PLACEHOLDER",
    "RequestIdFilter",
    "SecretRedactionFilter",
    "configure_logging",
]

#: What replaces a secret in the log stream.
REDACTION_PLACEHOLDER = "***REDACTED***"

#: `request_id` is present from day one so the format never changes when F02
#: starts correlating HTTP requests.
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: Shown when a record carries no correlation ID.
NO_REQUEST_ID = "-"

#: Below this length a "secret" is more likely to be a substring of ordinary
#: text than a credential; redacting it would corrupt every message.
MIN_REDACTABLE_LENGTH = 8

#: Marks handlers this module installed, so repeated configuration replaces
#: rather than accumulates.
_HANDLER_TAG = "_bi_agent_handler"


class RequestIdFilter(logging.Filter):
    """Give every record a ``request_id`` so the format string always resolves.

    Without this a single ``logging.info`` from library code raises KeyError
    inside the logging machinery — which surfaces as a broken app, not a broken
    log line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = NO_REQUEST_ID
        return True


class SecretRedactionFilter(logging.Filter):
    """Scrub known secret values out of every record before it is emitted.

    Covers the four ways a secret actually reaches a log line: interpolated into
    the message, passed as a lazy ``%s`` argument, nested inside a dict or JSON
    body being logged, and rendered into an exception traceback.
    """

    def __init__(self, secrets: Iterable[str | SecretStr | None] = ()) -> None:
        super().__init__()
        self._secrets: list[str] = []
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: str | SecretStr | None) -> None:
        """Register a value to scrub. Short, blank and duplicate values ignored."""
        if secret is None:
            return
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if not isinstance(value, str):
            return
        value = value.strip()
        if len(value) < MIN_REDACTABLE_LENGTH or value in self._secrets:
            return
        self._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        record.msg = self._scrub(record.msg)
        if record.args:
            record.args = self._scrub(record.args)

        # Tracebacks are rendered by the formatter from exc_info. Pre-rendering
        # into exc_text here means the formatter reuses our scrubbed text
        # instead of re-deriving the original.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._scrub_text(record.exc_text)
        if record.stack_info:
            record.stack_info = self._scrub_text(record.stack_info)

        return True

    # --- internals ---

    def _contains_secret(self, text: str) -> bool:
        return any(secret in text for secret in self._secrets)

    def _scrub_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTION_PLACEHOLDER)
        return text

    def _scrub(self, value: Any) -> Any:
        """Recursively redact, returning `value` unchanged when nothing matches.

        Identity preservation matters: the overwhelmingly common case is a log
        record with no secret in it, and that path must not allocate.
        """
        if isinstance(value, str):
            return self._scrub_text(value) if self._contains_secret(value) else value

        if isinstance(value, Mapping):
            scrubbed = {self._scrub(k): self._scrub(v) for k, v in value.items()}
            return scrubbed if scrubbed != value else value

        if isinstance(value, (list, tuple)):
            items = [self._scrub(item) for item in value]
            if all(new is old for new, old in zip(items, value)):
                return value
            return tuple(items) if isinstance(value, tuple) else items

        # Anything else — a dataclass, an httpx.Request, a custom object whose
        # repr embeds a header. We cannot rewrite it in place, so if its
        # rendering carries a secret we substitute the scrubbed rendering.
        if not isinstance(value, (int, float, bool, type(None))):
            try:
                text = str(value)
            except Exception:  # a broken __str__ must not break logging
                return value
            if self._contains_secret(text):
                return self._scrub_text(text)
        return value


def configure_logging(
    level: str | int = "INFO",
    *,
    secrets: Iterable[str | SecretStr | None] = (),
    stream: IO[str] | None = None,
) -> logging.Handler:
    """Install a single root handler with redaction. Safe to call repeatedly.

    Streamlit reruns the whole script on every user interaction, so without the
    replace-don't-append behaviour here, log output would multiply on every turn.
    """
    root = logging.getLogger()

    for handler in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT))
    handler.addFilter(RequestIdFilter())
    handler.addFilter(SecretRedactionFilter(secrets))
    setattr(handler, _HANDLER_TAG, True)

    root.addHandler(handler)
    root.setLevel(level)
    return handler
