"""F01 test plan cases 19-25: proof that a secret cannot reach the log stream.

These tests assert on the *emitted stream*, not on the filter's return value.
The guarantee that matters is "the token is not in the output", and only a real
handler with a real formatter can demonstrate that.
"""

from __future__ import annotations

import io
import logging

import pytest
from pydantic import SecretStr

from bi_agent.logging_config import (
    REDACTION_PLACEHOLDER,
    SecretRedactionFilter,
    configure_logging,
)
from tests.conftest import FAKE_MONDAY_TOKEN

TOKEN = FAKE_MONDAY_TOKEN


@pytest.fixture
def log_stream() -> tuple[logging.Logger, io.StringIO]:
    """A logger writing to a buffer through the redaction filter."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(SecretRedactionFilter([TOKEN]))

    logger = logging.getLogger("bi_agent.tests.redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    yield logger, stream

    logger.handlers = []


# --- 19: eagerly interpolated message ----------------------------------------


def test_token_interpolated_into_message_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    logger.info(f"calling monday with token {TOKEN}")

    output = stream.getvalue()
    assert TOKEN not in output
    assert REDACTION_PLACEHOLDER in output


# --- 20: lazy %s argument (the common real-world case) -----------------------


def test_token_passed_as_lazy_arg_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    logger.info("authorization=%s", TOKEN)

    output = stream.getvalue()
    assert TOKEN not in output
    assert REDACTION_PLACEHOLDER in output


def test_token_in_one_of_several_args_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    logger.info("board=%s auth=%s retries=%s", 12345, TOKEN, 3)

    output = stream.getvalue()
    assert TOKEN not in output
    assert "board=12345" in output
    assert "retries=3" in output


# --- 21: token inside a structure being logged -------------------------------


def test_token_inside_dict_arg_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    # A single mapping argument is stored verbatim as record.args by logging,
    # which is exactly how a request-header dump would arrive.
    logger.info("headers=%(headers)s", {"headers": {"Authorization": TOKEN}})

    output = stream.getvalue()
    assert TOKEN not in output
    assert REDACTION_PLACEHOLDER in output


def test_token_inside_nested_structure_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    body = {"errors": [{"message": "bad token", "context": {"auth": TOKEN}}]}
    logger.error("monday returned %s", body)

    output = stream.getvalue()
    assert TOKEN not in output
    assert REDACTION_PLACEHOLDER in output


def test_token_inside_a_logged_object_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    class Request:
        def __repr__(self) -> str:
            return f"Request(headers={{'Authorization': '{TOKEN}'}})"

    logger.error("failed request %s", Request())

    output = stream.getvalue()
    assert TOKEN not in output
    assert REDACTION_PLACEHOLDER in output


# --- 22: token inside a logged traceback -------------------------------------


def test_token_in_exception_traceback_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    try:
        raise ValueError(f"auth rejected for token {TOKEN}")
    except ValueError:
        logger.exception("request failed")

    output = stream.getvalue()
    assert "Traceback" in output
    assert TOKEN not in output
    assert REDACTION_PLACEHOLDER in output


# --- 23: normal logs are untouched -------------------------------------------


def test_unrelated_messages_pass_through_unmodified(log_stream) -> None:
    logger, stream = log_stream

    logger.info("fetched %s items from board %s", 344, "Deals")

    assert stream.getvalue().strip() == "INFO fetched 344 items from board Deals"


def test_filter_preserves_args_identity_when_nothing_matches() -> None:
    """No allocation and no mutation on the overwhelmingly common path."""
    args = ({"board": "Deals"},)
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "board=%s", args, None
    )
    original = record.args

    SecretRedactionFilter([TOKEN]).filter(record)

    assert record.args is original


# --- 24: no secrets configured ------------------------------------------------


def test_filter_with_no_secrets_is_a_noop() -> None:
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "token=%s", (TOKEN,), None
    )

    assert SecretRedactionFilter([]).filter(record) is True
    assert record.getMessage() == f"token={TOKEN}"


def test_filter_ignores_empty_and_none_secrets() -> None:
    filt = SecretRedactionFilter([None, "", "   ", TOKEN])
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "a b c", None, None
    )

    assert filt.filter(record) is True
    assert record.getMessage() == "a b c"


def test_filter_ignores_implausibly_short_secrets() -> None:
    """Redacting a 2-character 'secret' would corrupt every message."""
    filt = SecretRedactionFilter(["ab"])
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "fetched a board", None, None
    )

    filt.filter(record)

    assert record.getMessage() == "fetched a board"


def test_add_secret_after_construction() -> None:
    filt = SecretRedactionFilter()
    filt.add_secret(TOKEN)
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "token=%s", (TOKEN,), None
    )

    filt.filter(record)

    assert TOKEN not in record.getMessage()


# --- 25: configure_logging is idempotent -------------------------------------


def test_configure_logging_does_not_duplicate_handlers() -> None:
    root = logging.getLogger()
    original = list(root.handlers)
    try:
        configure_logging(level="INFO")
        after_first = len(root.handlers)

        configure_logging(level="DEBUG")

        assert len(root.handlers) == after_first
        assert root.level == logging.DEBUG
    finally:
        root.handlers = original


def test_configure_logging_emits_through_the_redaction_filter() -> None:
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    stream = io.StringIO()
    try:
        configure_logging(level="INFO", secrets=[TOKEN], stream=stream)
        logging.getLogger("bi_agent.test").info("token=%s", TOKEN)

        output = stream.getvalue()
        assert TOKEN not in output
        assert REDACTION_PLACEHOLDER in output
    finally:
        root.handlers = original
        root.setLevel(original_level)


def test_configured_format_tolerates_records_without_a_request_id() -> None:
    """request_id is in the format from day one so F02 does not change it.

    Records that never set it must still format, rather than raising KeyError
    inside the logging machinery.
    """
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    stream = io.StringIO()
    try:
        configure_logging(level="INFO", stream=stream)
        logging.getLogger("bi_agent.test").info("no request id here")

        assert "no request id here" in stream.getvalue()
    finally:
        root.handlers = original
        root.setLevel(original_level)


def test_configured_format_includes_an_explicit_request_id() -> None:
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    stream = io.StringIO()
    try:
        configure_logging(level="INFO", stream=stream)
        logging.getLogger("bi_agent.test").info(
            "fetching board", extra={"request_id": "req-42"}
        )

        assert "req-42" in stream.getvalue()
    finally:
        root.handlers = original
        root.setLevel(original_level)


# --- remaining scrub paths ----------------------------------------------------


def test_token_in_stack_info_is_redacted(log_stream) -> None:
    logger, stream = log_stream

    def call_with_token(token: str) -> None:
        logger.info("tracing %s", token, stack_info=True)

    call_with_token(TOKEN)

    output = stream.getvalue()
    assert "Stack (most recent call last)" in output
    assert TOKEN not in output
    assert REDACTION_PLACEHOLDER in output


def test_object_without_a_secret_is_passed_through_untouched(log_stream) -> None:
    logger, stream = log_stream

    payload = {"board": "Deals", "items": 344}
    logger.info("fetched %s", payload)

    assert "'board': 'Deals'" in stream.getvalue()


def test_object_with_a_broken_repr_does_not_break_logging() -> None:
    """A logging filter that can raise is worse than no filter at all."""

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no repr for you")

        __repr__ = __str__

    hostile = Hostile()
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "value=%s", (hostile,), None
    )

    assert SecretRedactionFilter([TOKEN]).filter(record) is True
    assert record.args == (hostile,)


def test_non_string_secret_is_ignored() -> None:
    filt = SecretRedactionFilter()
    filt.add_secret(12345)  # type: ignore[arg-type]

    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "id=%s", (12345,), None
    )
    filt.filter(record)

    assert record.getMessage() == "id=12345"


def test_secretstr_is_accepted_as_a_secret_source() -> None:
    filt = SecretRedactionFilter([SecretStr(TOKEN)])
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "auth=%s", (TOKEN,), None
    )

    filt.filter(record)

    assert TOKEN not in record.getMessage()


def test_duplicate_secrets_are_registered_once() -> None:
    filt = SecretRedactionFilter([TOKEN, TOKEN, SecretStr(TOKEN)])
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "auth=%s", (TOKEN,), None
    )

    filt.filter(record)

    assert record.getMessage() == f"auth={REDACTION_PLACEHOLDER}"


def test_custom_object_without_a_secret_keeps_its_own_repr(log_stream) -> None:
    """The common case for a non-primitive arg: leave the object alone so the
    formatter renders it, rather than pre-stringifying every log argument."""
    logger, stream = log_stream

    class Board:
        def __repr__(self) -> str:
            return "Board(name='Deals', items=344)"

    board = Board()
    logger.info("resolved %s", board)

    assert "Board(name='Deals', items=344)" in stream.getvalue()
