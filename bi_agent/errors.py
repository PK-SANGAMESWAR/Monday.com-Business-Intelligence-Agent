"""Typed exception hierarchy.

Every error the agent can raise lives here and descends from :class:`BIAgentError`.
The load-bearing detail is :attr:`BIAgentError.user_message`: each exception knows
how it should be described to a founder, separately from the developer-facing
``str(exc)``. That is what makes FR-16 ("graceful handling of API failures")
testable — "graceful degradation" becomes an assertion on a specific string
rather than a vibe.

Two rules hold for every ``user_message`` in this module:

* it never contains a secret, a URL with credentials, or a raw payload;
* it says what the agent will do next, not just what broke.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BIAgentError",
    "ConfigError",
    "DataError",
    "LLMError",
    "MondayAuthError",
    "MondayError",
    "MondayQueryError",
    "MondayRateLimitError",
    "MondayUnavailableError",
    "NormalizationError",
    "QuerySpecError",
    "ReadOnlyViolationError",
    "SchemaMismatchError",
]


class BIAgentError(Exception):
    """Base class for every error this agent raises deliberately.

    Catching :class:`BIAgentError` catches everything we know how to explain;
    anything else escaping is a genuine bug and should not be swallowed.
    """

    #: Founder-facing description. Subclasses override; instances may too.
    default_user_message = (
        "Something went wrong while answering that question. "
        "Nothing on your boards was changed."
    )

    def __init__(self, message: str = "", *, user_message: str | None = None) -> None:
        super().__init__(message)
        self._user_message = user_message

    @property
    def user_message(self) -> str:
        """What to show the user. Never contains secrets or raw payloads."""
        return self._user_message or self.default_user_message


class ConfigError(BIAgentError):
    """Configuration is missing or invalid. Raised at startup, before any work."""

    default_user_message = (
        "The agent is not configured correctly, so it cannot start. "
        "See the setup steps in the README."
    )


# --- monday.com ---------------------------------------------------------------


class MondayError(BIAgentError):
    """Any failure talking to monday.com.

    Grouping matters: the degradation strategy in plan section 4.3 catches this
    class to fall back to cache, so every monday failure must land underneath it.
    """

    default_user_message = "I could not read your monday.com boards just now."


class MondayAuthError(MondayError):
    """401, or a token the API refuses. Not retryable."""

    default_user_message = (
        "I cannot authenticate to monday.com. The API token looks missing, "
        "expired, or not permitted for these boards."
    )


class MondayRateLimitError(MondayError):
    """429, or the GraphQL complexity budget exhausted. Retryable with backoff."""

    default_user_message = (
        "monday.com is rate-limiting our requests. I will back off and retry; "
        "if I have recent data cached I will use it and tell you how old it is."
    )

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: float | None = None,
        user_message: str | None = None,
    ) -> None:
        super().__init__(message, user_message=user_message)
        #: Seconds to wait, when the API tells us. ``None`` when it does not.
        self.retry_after = retry_after


class MondayUnavailableError(MondayError):
    """5xx, timeout, DNS or connection failure. Retryable."""

    default_user_message = (
        "monday.com did not respond. I will answer from cached data where I can "
        "and say plainly what is missing."
    )


class MondayQueryError(MondayError):
    """HTTP 200 carrying a GraphQL ``errors[]`` array."""

    default_user_message = (
        "monday.com rejected the request for that board data, so this answer "
        "may be incomplete."
    )


class SchemaMismatchError(MondayError):
    """A column the agent expects is absent from the board.

    Degradation is per-field, not per-board (FR-9), so the missing columns are
    named in the user message: the user needs to know which figure went away.
    """

    default_user_message = (
        "A column I expected is missing from the board, so part of this answer "
        "is unavailable."
    )

    def __init__(
        self,
        message: str = "",
        *,
        missing: list[str] | None = None,
        user_message: str | None = None,
    ) -> None:
        self.missing = list(missing or [])
        if user_message is None and self.missing:
            named = ", ".join(self.missing)
            user_message = (
                f"These columns are missing from the board: {named}. "
                "I will answer using the fields that remain."
            )
        super().__init__(message, user_message=user_message)


class ReadOnlyViolationError(MondayError):
    """A mutating GraphQL operation was assembled. Raised before it is sent.

    Defined at the foundation because FR-5 is a hard constraint; the exception
    guarding it should not be an afterthought bolted on beside the code that
    would violate it. Raised in F02.
    """

    default_user_message = (
        "Blocked: this agent is read-only and never modifies your monday.com "
        "boards. The attempted write was stopped before it was sent."
    )


# --- data ---------------------------------------------------------------------


class DataError(BIAgentError):
    """Board data could not be normalized or interpreted."""

    default_user_message = (
        "Some board records could not be processed, so this answer covers "
        "fewer rows than the full board."
    )


class NormalizationError(DataError):
    """A single value defeated its parser."""

    default_user_message = (
        "A value on the board could not be interpreted, so that record is "
        "excluded from this figure."
    )

    def __init__(
        self,
        message: str = "",
        *,
        field: str | None = None,
        raw_value: Any = None,
        user_message: str | None = None,
    ) -> None:
        super().__init__(message, user_message=user_message)
        #: Canonical field name that failed to parse.
        self.field = field
        #: The offending value, kept for the data-quality report.
        self.raw_value = raw_value


# --- agent --------------------------------------------------------------------


class QuerySpecError(BIAgentError):
    """The model sent a query specification that failed validation.

    :attr:`hint` is addressed to the *model*, not the user: F06 feeds it back as
    a tool error so the model can correct itself. The user should normally never
    see this exception at all.
    """

    default_user_message = (
        "I could not run that query as specified. Let me try a different angle."
    )

    default_hint = (
        "The query spec was rejected. Call describe_data to list the valid "
        "fields, operators and values for this board, then retry."
    )

    def __init__(
        self,
        message: str = "",
        *,
        hint: str | None = None,
        user_message: str | None = None,
    ) -> None:
        super().__init__(message, user_message=user_message)
        #: A correction addressed to the model, returned as a tool error.
        self.hint = hint or self.default_hint


class LLMError(BIAgentError):
    """The Anthropic API failed."""

    default_user_message = (
        "The reasoning service failed, so I could not complete that answer. "
        "Your conversation is preserved - please try again."
    )
