"""Tests for bi_agent/agent/loop.py, against a stubbed Anthropic client.

No `ANTHROPIC_API_KEY` is needed or read: `Agent(client=...)` bypasses `_build_client`
entirely, matching the injection pattern F02's `MondayClient`/F03's `SeedWriter` already
use for their own transports.
"""

from __future__ import annotations

import pytest

from bi_agent.agent.loop import MAX_TOOL_ITERATIONS, Agent
from bi_agent.errors import LLMError


class FakeBlock:
    def __init__(self, type_: str, **kwargs):
        self.type = type_
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, content: list[FakeBlock]):
        self.content = content


class FakeMessages:
    def __init__(self, responses: list[FakeResponse | Exception]):
        self._responses = iter(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = next(self._responses)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, responses: list[FakeResponse | Exception]):
        self.messages = FakeMessages(responses)


@pytest.fixture
def agent_factory(board_repository, settings_factory):
    def _make(responses: list[FakeResponse | Exception]) -> tuple[Agent, FakeClient]:
        client = FakeClient(responses)
        agent = Agent(board_repository, settings_factory(), client=client)
        return agent, client

    return _make


def test_single_turn_text_answer_no_tool_call(agent_factory):
    agent, client = agent_factory([FakeResponse([FakeBlock("text", text="Hello, founder.")])])
    result = agent.ask("hi")
    assert result.text == "Hello, founder."
    assert result.tool_calls == []
    assert len(client.messages.calls) == 1


def test_clarifying_question_is_returned_without_forcing_a_tool_call(agent_factory):
    question = "Do you mean billed revenue, deal value, or collected cash?"
    agent, _client = agent_factory([FakeResponse([FakeBlock("text", text=question)])])
    result = agent.ask("how's revenue?")
    assert result.text == question
    assert result.tool_calls == []


def test_tool_call_round_then_final_answer(agent_factory):
    agent, client = agent_factory(
        [
            FakeResponse(
                [FakeBlock("tool_use", id="t1", name="query_deals", input={"metric": "count"})]
            ),
            FakeResponse([FakeBlock("text", text="There are 344 real deals.")]),
        ]
    )
    result = agent.ask("how many deals do we have?")
    assert result.text == "There are 344 real deals."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "query_deals"
    assert result.tool_calls[0]["result"]["value"] == 344
    assert len(client.messages.calls) == 2


def test_multiple_tool_calls_in_one_round_are_all_dispatched(agent_factory):
    agent, _client = agent_factory(
        [
            FakeResponse(
                [
                    FakeBlock("tool_use", id="t1", name="query_deals", input={"metric": "count"}),
                    FakeBlock(
                        "tool_use",
                        id="t2",
                        name="query_work_orders",
                        input={"metric": "count"},
                    ),
                ]
            ),
            FakeResponse([FakeBlock("text", text="344 deals, 176 work orders.")]),
        ]
    )
    result = agent.ask("give me both counts")
    assert len(result.tool_calls) == 2
    assert {call["name"] for call in result.tool_calls} == {"query_deals", "query_work_orders"}


def test_invalid_tool_call_returns_a_correctable_result_not_a_crash(agent_factory):
    agent, _client = agent_factory(
        [
            FakeResponse(
                [
                    FakeBlock(
                        "tool_use",
                        id="t1",
                        name="query_deals",
                        input={"metric": "sum", "field": "sector"},
                    )
                ]
            ),
            FakeResponse([FakeBlock("text", text="Let me try a different field.")]),
        ]
    )
    result = agent.ask("sum the sectors")
    assert "error" in result.tool_calls[0]["result"]


def test_api_failure_raises_llm_error_and_preserves_conversation(agent_factory):
    agent, _client = agent_factory([RuntimeError("connection reset")])
    with pytest.raises(LLMError):
        agent.ask("hello")
    # The user's turn is preserved even though the call failed.
    assert agent._messages[0] == {"role": "user", "content": "hello"}


def test_loop_that_never_converges_raises_llm_error(agent_factory):
    endless = [
        FakeResponse([FakeBlock("tool_use", id=f"t{i}", name="query_deals", input={"metric": "count"})])
        for i in range(MAX_TOOL_ITERATIONS + 1)
    ]
    agent, client = agent_factory(endless)
    with pytest.raises(LLMError):
        agent.ask("keep going forever")
    assert len(client.messages.calls) == MAX_TOOL_ITERATIONS


def test_second_ask_continues_the_same_conversation(agent_factory):
    agent, client = agent_factory(
        [
            FakeResponse([FakeBlock("text", text="First answer.")]),
            FakeResponse([FakeBlock("text", text="Second answer.")]),
        ]
    )
    agent.ask("first question")
    agent.ask("second question")
    # Both user turns and both assistant turns are in the running conversation.
    roles = [message["role"] for message in agent._messages]
    assert roles == ["user", "assistant", "user", "assistant"]
