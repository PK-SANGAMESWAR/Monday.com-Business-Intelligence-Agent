"""F01 test plan cases 12-18: the exception hierarchy and its user_message contract."""

from __future__ import annotations

import inspect
import re

import pytest

from bi_agent import errors
from bi_agent.errors import (
    BIAgentError,
    ConfigError,
    DataError,
    LLMError,
    MondayAuthError,
    MondayError,
    MondayQueryError,
    MondayRateLimitError,
    MondayUnavailableError,
    NormalizationError,
    QuerySpecError,
    ReadOnlyViolationError,
    SchemaMismatchError,
)


def _exception_classes() -> list[type[Exception]]:
    """Every exception class defined in bi_agent.errors."""
    return [
        obj
        for _, obj in inspect.getmembers(errors, inspect.isclass)
        if issubclass(obj, BaseException) and obj.__module__ == errors.__name__
    ]


def _concrete_classes() -> list[type[BIAgentError]]:
    """Leaf errors — the ones actually raised."""
    return [
        cls
        for cls in _exception_classes()
        if issubclass(cls, BIAgentError) and not cls.__subclasses__()
    ]


# --- 12: nothing escapes the hierarchy ---------------------------------------


def test_every_error_in_the_module_descends_from_the_base() -> None:
    classes = _exception_classes()

    assert classes, "no exception classes found - the import is wrong"
    for cls in classes:
        assert issubclass(cls, BIAgentError), (
            f"{cls.__name__} is defined outside the BIAgentError hierarchy; "
            "callers that catch BIAgentError would miss it"
        )


def test_hierarchy_shape_matches_the_design() -> None:
    assert issubclass(ConfigError, BIAgentError)
    assert issubclass(MondayError, BIAgentError)
    assert issubclass(DataError, BIAgentError)
    assert issubclass(QuerySpecError, BIAgentError)
    assert issubclass(LLMError, BIAgentError)

    for cls in (
        MondayAuthError,
        MondayRateLimitError,
        MondayUnavailableError,
        MondayQueryError,
        SchemaMismatchError,
        ReadOnlyViolationError,
    ):
        assert issubclass(cls, MondayError)

    assert issubclass(NormalizationError, DataError)


# --- 13: every error can describe itself to a founder ------------------------


def test_every_concrete_error_has_a_non_empty_user_message() -> None:
    for cls in _concrete_classes():
        exc = cls("developer-facing detail")
        assert isinstance(exc.user_message, str)
        assert exc.user_message.strip(), f"{cls.__name__} has an empty user_message"


def test_user_message_is_separate_from_developer_message() -> None:
    exc = MondayAuthError("401 from https://api.monday.com/v2 for board 123")

    assert str(exc) == "401 from https://api.monday.com/v2 for board 123"
    assert exc.user_message != str(exc)


def test_user_message_can_be_overridden_per_instance() -> None:
    exc = MondayUnavailableError("timeout", user_message="Deals board is unreachable.")

    assert exc.user_message == "Deals board is unreachable."


def test_base_error_defaults_user_message_when_subclass_defines_none() -> None:
    assert BIAgentError("boom").user_message.strip()


# --- 14: no secret-shaped text in anything shown to a user -------------------


#: Long unbroken runs of token-ish characters - what an API key looks like.
SECRET_SHAPE = re.compile(r"[A-Za-z0-9_\-]{25,}|eyJ[A-Za-z0-9_\-]+")


def test_user_messages_contain_nothing_secret_shaped() -> None:
    for cls in _concrete_classes():
        message = cls("developer-facing detail").user_message
        assert not SECRET_SHAPE.search(message), (
            f"{cls.__name__}.user_message contains a secret-shaped substring: {message!r}"
        )


def test_secret_shape_regex_actually_matches_a_token() -> None:
    """Guard the guard - a regex that matches nothing would pass test 14 vacuously."""
    assert SECRET_SHAPE.search("eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnopqrstuvwxyz012345")


# --- 15: rate-limit retry_after ----------------------------------------------


def test_rate_limit_retry_after_preserved() -> None:
    assert MondayRateLimitError("429", retry_after=30).retry_after == 30


def test_rate_limit_retry_after_defaults_to_none() -> None:
    assert MondayRateLimitError("429").retry_after is None


# --- 16: schema mismatch names the missing columns ---------------------------


def test_schema_mismatch_lists_missing_columns() -> None:
    exc = SchemaMismatchError("board 123", missing=["Deal Value", "Close Date"])

    assert exc.missing == ["Deal Value", "Close Date"]
    # FR-9: degrade per-field, so the user must be told which field went away.
    assert "Deal Value" in exc.user_message
    assert "Close Date" in exc.user_message


def test_schema_mismatch_missing_defaults_to_empty_list() -> None:
    assert SchemaMismatchError("board 123").missing == []


# --- 17: query-spec hint is addressed to the model ---------------------------


def test_query_spec_error_carries_a_hint() -> None:
    exc = QuerySpecError(
        "unknown field 'revenue'",
        hint="Field 'revenue' does not exist. Call describe_data for valid fields.",
    )

    assert "describe_data" in exc.hint


def test_query_spec_error_has_a_default_hint() -> None:
    assert QuerySpecError("bad spec").hint.strip()


# --- 18: catching MondayError catches the whole monday family ----------------


@pytest.mark.parametrize(
    "cls",
    [
        MondayAuthError,
        MondayRateLimitError,
        MondayUnavailableError,
        MondayQueryError,
        SchemaMismatchError,
        ReadOnlyViolationError,
    ],
)
def test_monday_subclasses_are_caught_as_monday_error(cls: type[MondayError]) -> None:
    with pytest.raises(MondayError):
        raise cls("boom")


def test_all_bi_agent_errors_are_caught_as_base() -> None:
    for cls in _concrete_classes():
        with pytest.raises(BIAgentError):
            raise cls("boom")


# --- supporting behaviour ----------------------------------------------------


def test_normalization_error_carries_field_and_raw_value() -> None:
    exc = NormalizationError("could not parse", field="deal_value", raw_value="#VALUE!")

    assert exc.field == "deal_value"
    assert exc.raw_value == "#VALUE!"


def test_read_only_violation_names_the_constraint() -> None:
    """FR-5 is a hard constraint; the message must say so plainly."""
    message = ReadOnlyViolationError("mutation detected").user_message.lower()

    assert "read-only" in message or "read only" in message
