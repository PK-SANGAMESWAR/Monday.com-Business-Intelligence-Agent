"""Integration tests for app.py (F09), run in-process via `streamlit.testing.v1.AppTest`.

No test performs a real HTTP call or reaches a real LLM: the monday.com transport is
intercepted by `respx` exactly as `tests/conftest.py`'s `board_repository` fixture does
one layer down, and the happy-path conversation monkeypatches `anthropic.Anthropic` with
the same stub-message shapes `tests/unit/test_loop.py` already uses.

`st.cache_resource` (app.py's `_build_repository`) is a *process-wide* cache, so it is
cleared before and after every test here - otherwise one test's mocked transport would
leak into the next test's `BoardRepository` instance.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tests.conftest import FAKE_ANTHROPIC_KEY, FAKE_MONDAY_TOKEN, LIVE_FIXTURE_DIR

APP_FILE = str(Path(__file__).resolve().parents[2] / "app.py")


@pytest.fixture(autouse=True)
def _clear_resource_cache():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture
def mocked_boards(respx_mock):
    """Route ListBoards/BoardItemsFirst to the recorded fixtures, exactly like
    `tests/conftest.py::board_repository` but keyed off `get_settings()`'s default URL
    rather than a `settings_factory()` instance, since app.py never accepts injected
    settings.
    """
    list_boards = json.loads((LIVE_FIXTURE_DIR / "list_boards.json").read_text(encoding="utf-8"))
    deals_payload = json.loads(
        (LIVE_FIXTURE_DIR / "deals_board_items.json").read_text(encoding="utf-8")
    )
    wo_payload = json.loads(
        (LIVE_FIXTURE_DIR / "work_orders_board_items.json").read_text(encoding="utf-8")
    )
    by_board_id = {
        deals_payload["data"]["boards"][0]["id"]: deals_payload,
        wo_payload["data"]["boards"][0]["id"]: wo_payload,
    }

    def _handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, variables = body["query"], body.get("variables") or {}
        if "ListBoards" in query:
            return httpx.Response(200, json=list_boards)
        if "BoardItemsFirst" in query:
            board_id = str(variables["boardIds"][0])
            return httpx.Response(200, json=by_board_id[board_id])
        raise AssertionError(f"unhandled query in test_app mock: {query[:80]!r}")

    respx_mock.post("https://api.monday.com/v2").mock(side_effect=_handle)
    return respx_mock


@pytest.fixture
def monday_only(monkeypatch, mocked_boards):
    """MONDAY_API_KEY set, ANTHROPIC_API_KEY absent - this environment's actual state."""
    monkeypatch.setenv("MONDAY_API_KEY", FAKE_MONDAY_TOKEN)
    return mocked_boards


class FakeBlock:
    def __init__(self, type_: str, **kwargs):
        self.type = type_
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeAnthropicResponse:
    def __init__(self, content):
        self.content = content


class FakeAnthropicMessages:
    """Pops from a list shared with `FakeAnthropicClient`, not an iterator captured at
    construction time - `Agent` is built once per session and its `FakeAnthropicMessages`
    must keep seeing responses a test queues for a *later* turn in the same conversation
    (see `test_two_turns_both_remain_in_the_rendered_history`)."""

    def __init__(self, responses: list) -> None:
        self._responses = responses

    def create(self, **kwargs):
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeAnthropicClient:
    """Stands in for `anthropic.Anthropic(api_key=...)`. `Agent._build_client` only
    ever calls the constructor with `api_key=`, so that is the only signature honored.
    """

    _next_responses: list = []

    def __init__(self, api_key: str) -> None:
        self.messages = FakeAnthropicMessages(FakeAnthropicClient._next_responses)


@pytest.fixture(autouse=True)
def _reset_fake_anthropic_queue():
    FakeAnthropicClient._next_responses = []


@pytest.fixture
def monday_and_anthropic(monkeypatch, mocked_boards):
    """Both credentials present, with a stubbed Anthropic SDK client so the happy path
    runs an actual `Agent.ask()` round-trip with no network and no real API key."""
    monkeypatch.setenv("MONDAY_API_KEY", FAKE_MONDAY_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC_KEY)
    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropicClient)
    return mocked_boards


def _queue_anthropic_responses(*responses: FakeAnthropicResponse) -> None:
    FakeAnthropicClient._next_responses.extend(responses)


# --- degradation paths ---------------------------------------------------------


def test_config_error_stops_before_any_board_call(monkeypatch, mocked_boards):
    """No MONDAY_API_KEY at all: the page must show the config error and never touch
    the mocked transport."""
    at = AppTest.from_file(APP_FILE)
    at.run()

    assert not at.exception
    assert any("Configuration" in e.value for e in at.error)
    assert mocked_boards.calls.call_count == 0


def test_missing_anthropic_key_disables_chat_but_not_data_quality(monday_only):
    at = AppTest.from_file(APP_FILE)
    at.run()

    assert not at.exception
    assert len(at.chat_input) == 1
    assert at.chat_input[0].disabled is True
    assert len(at.warning) == 1

    sidebar_text = " ".join(md.value for md in at.sidebar.markdown) + " ".join(
        c.value for c in at.sidebar.caption
    )
    assert "Deals" in sidebar_text
    assert "Work Orders" in sidebar_text
    assert "346" in sidebar_text or "344" in sidebar_text  # some row count rendered


def test_refresh_button_clears_the_repository_cache(monday_only):
    at = AppTest.from_file(APP_FILE)
    at.run()

    calls_before = monday_only.calls.call_count
    at.sidebar.button[0].click().run()

    assert not at.exception
    assert len(at.sidebar.success) == 1
    # Sidebar re-render re-reads both boards through the (now-invalidated) cache,
    # so at least one more request must have gone out.
    assert monday_only.calls.call_count > calls_before


# --- happy path with a stubbed Anthropic client ---------------------------------


def test_single_turn_answer_with_no_tool_call(monday_and_anthropic):
    _queue_anthropic_responses(
        FakeAnthropicResponse([FakeBlock("text", text="You have 344 deals on record.")])
    )
    at = AppTest.from_file(APP_FILE)
    at.run()
    assert at.chat_input[0].disabled is False

    at.chat_input[0].set_value("how many deals do we have?").run()

    assert not at.exception
    messages = at.chat_message
    assert messages[0].name == "user"
    assert "how many deals" in messages[0].markdown[0].value
    assert messages[1].name == "assistant"
    assert "You have 344 deals on record." in messages[1].markdown[0].value
    assert len(messages[1].expander) == 0  # no tool call -> no "data behind this" panel


def test_answer_with_a_tool_call_renders_the_metric_result(monday_and_anthropic):
    _queue_anthropic_responses(
        FakeAnthropicResponse(
            [FakeBlock("tool_use", id="t1", name="query_deals", input={"metric": "count"})]
        ),
        FakeAnthropicResponse([FakeBlock("text", text="There are 344 real deals.")]),
    )
    at = AppTest.from_file(APP_FILE)
    at.run()
    at.chat_input[0].set_value("how many deals?").run()

    assert not at.exception
    assistant = at.chat_message[1]
    assert "There are 344 real deals." in assistant.markdown[0].value
    assert len(assistant.expander) == 1
    assert "query_deals" in assistant.expander[0].markdown[0].value
    result = assistant.expander[0].json[0].value
    assert json.loads(result)["value"] == 344


def test_tool_use_loop_error_is_shown_as_the_assistant_turn_not_a_crash(monday_and_anthropic):
    _queue_anthropic_responses(RuntimeError("connection reset"))
    at = AppTest.from_file(APP_FILE)
    at.run()
    at.chat_input[0].set_value("hello").run()

    assert not at.exception
    assistant = at.chat_message[1]
    assert "reasoning service failed" in assistant.markdown[0].value.lower()


def test_two_turns_both_remain_in_the_rendered_history(monday_and_anthropic):
    _queue_anthropic_responses(FakeAnthropicResponse([FakeBlock("text", text="First answer.")]))
    at = AppTest.from_file(APP_FILE)
    at.run()
    at.chat_input[0].set_value("first question").run()

    _queue_anthropic_responses(FakeAnthropicResponse([FakeBlock("text", text="Second answer.")]))
    at.chat_input[0].set_value("second question").run()

    assert not at.exception
    texts = [m.markdown[0].value for m in at.chat_message]
    assert "first question" in texts[0]
    assert "First answer." in texts[1]
    assert "second question" in texts[2]
    assert "Second answer." in texts[3]
