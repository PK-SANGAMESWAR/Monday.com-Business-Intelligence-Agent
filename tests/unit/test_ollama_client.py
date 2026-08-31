"""Tests for `bi_agent/agent/ollama_client.py`.

Two layers: pure translation functions (Anthropic-shaped tools/messages <-> Ollama's
`/api/chat` shapes), tested without any HTTP at all, and `OllamaClient` itself against a
mocked transport (`respx`), so the suite stays offline (NFR-3) and never requires a real
Ollama daemon.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from bi_agent.agent.ollama_client import (
    OllamaClient,
    _assistant_turn_message,
    _parse_response,
    _to_ollama_messages,
    _to_ollama_tools,
)
from bi_agent.errors import LLMError

BASE_URL = "http://localhost:11434"


# --- pure translation: tools ---------------------------------------------------


def test_tool_schema_translated_to_ollama_function_shape():
    anthropic_tools = [
        {
            "name": "query_deals",
            "description": "Query the deals board.",
            "input_schema": {"type": "object", "properties": {"metric": {"type": "string"}}},
        }
    ]

    ollama_tools = _to_ollama_tools(anthropic_tools)

    assert ollama_tools == [
        {
            "type": "function",
            "function": {
                "name": "query_deals",
                "description": "Query the deals board.",
                "parameters": {"type": "object", "properties": {"metric": {"type": "string"}}},
            },
        }
    ]


def test_tool_schema_translation_defaults_missing_description():
    ollama_tools = _to_ollama_tools([{"name": "x", "input_schema": {"type": "object"}}])
    assert ollama_tools[0]["function"]["description"] == ""


# --- pure translation: messages -------------------------------------------------


def test_plain_string_user_turn_passes_through():
    converted = _to_ollama_messages("be helpful", [{"role": "user", "content": "hi"}])
    assert converted == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
    ]


def test_assistant_text_only_turn_has_no_tool_calls_key():
    content = [SimpleNamespace(type="text", text="There are 344 deals.")]
    message = _assistant_turn_message("assistant", content)
    assert message == {"role": "assistant", "content": "There are 344 deals."}
    assert "tool_calls" not in message


def test_assistant_tool_use_turn_carries_tool_calls():
    content = [
        SimpleNamespace(type="text", text=""),
        SimpleNamespace(type="tool_use", id="ollama_call_0", name="query_deals", input={"metric": "count"}),
    ]
    message = _assistant_turn_message("assistant", content)
    assert message["tool_calls"] == [{"function": {"name": "query_deals", "arguments": {"metric": "count"}}}]


def test_tool_result_turn_becomes_one_tool_role_message_per_result():
    tool_results = [
        {"type": "tool_result", "tool_use_id": "ollama_call_0", "content": '{"value": 344}'},
        {"type": "tool_result", "tool_use_id": "ollama_call_1", "content": '{"value": 176}'},
    ]
    converted = _to_ollama_messages("sys", [{"role": "user", "content": tool_results}])

    assert converted[1:] == [
        {"role": "tool", "content": '{"value": 344}'},
        {"role": "tool", "content": '{"value": 176}'},
    ]


def test_full_conversation_round_trip_preserves_order():
    messages = [
        {"role": "user", "content": "how many deals?"},
        {
            "role": "assistant",
            "content": [
                SimpleNamespace(type="tool_use", id="ollama_call_0", name="query_deals", input={"metric": "count"})
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "ollama_call_0", "content": "344"}],
        },
    ]
    converted = _to_ollama_messages("sys", messages)
    assert [m["role"] for m in converted] == ["system", "user", "assistant", "tool"]


# --- pure translation: responses -------------------------------------------------


def test_text_only_response_parsed_to_single_text_block():
    response = _parse_response({"message": {"role": "assistant", "content": "Hello, founder."}})
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    assert response.content[0].text == "Hello, founder."


def test_tool_call_response_parsed_with_synthesized_ids():
    payload = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "query_deals", "arguments": {"metric": "count"}}},
                {"function": {"name": "query_work_orders", "arguments": {"metric": "count"}}},
            ],
        }
    }
    response = _parse_response(payload)
    tool_blocks = [b for b in response.content if b.type == "tool_use"]
    assert [b.id for b in tool_blocks] == ["ollama_call_0", "ollama_call_1"]
    assert [b.name for b in tool_blocks] == ["query_deals", "query_work_orders"]
    assert tool_blocks[0].input == {"metric": "count"}


def test_tool_call_arguments_as_json_string_are_parsed():
    payload = {
        "message": {
            "tool_calls": [{"function": {"name": "query_deals", "arguments": '{"metric": "count"}'}}]
        }
    }
    response = _parse_response(payload)
    assert response.content[0].input == {"metric": "count"}


def test_malformed_json_string_arguments_degrade_to_empty_dict():
    payload = {"message": {"tool_calls": [{"function": {"name": "query_deals", "arguments": "{not json"}}]}}
    response = _parse_response(payload)
    assert response.content[0].input == {}


def test_empty_message_still_yields_one_text_block_so_loop_does_not_crash():
    response = _parse_response({"message": {}})
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    assert response.content[0].text == ""


# --- OllamaClient against a mocked transport -------------------------------------


@pytest.fixture
def ollama_client(respx_mock):
    client = OllamaClient(base_url=BASE_URL, timeout=5.0)
    yield client
    client.close()


def test_create_sends_translated_payload_and_parses_text_response(respx_mock, ollama_client):
    route = respx_mock.post(f"{BASE_URL}/api/chat")
    route.mock(
        return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "There are 344 deals."}}
        )
    )

    response = ollama_client.messages.create(
        model="llama3.1",
        max_tokens=1024,
        system="be helpful",
        tools=[{"name": "query_deals", "description": "d", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": "how many deals?"}],
    )

    assert response.content[0].text == "There are 344 deals."
    sent = route.calls.last.request
    import json as _json

    body = _json.loads(sent.content)
    assert body["model"] == "llama3.1"
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "be helpful"}
    assert body["tools"][0]["function"]["name"] == "query_deals"


def test_connection_error_raises_llm_error_with_actionable_message(respx_mock, ollama_client):
    respx_mock.post(f"{BASE_URL}/api/chat").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(LLMError) as excinfo:
        ollama_client.messages.create(
            model="llama3.1", max_tokens=10, system="s", tools=[], messages=[]
        )

    assert "ollama serve" in excinfo.value.user_message


def test_http_error_status_names_the_model_in_the_message(respx_mock, ollama_client):
    respx_mock.post(f"{BASE_URL}/api/chat").mock(
        return_value=httpx.Response(404, text="model not found")
    )

    with pytest.raises(LLMError) as excinfo:
        ollama_client.messages.create(
            model="not-pulled", max_tokens=10, system="s", tools=[], messages=[]
        )

    assert "not-pulled" in str(excinfo.value)


def test_non_json_response_raises_llm_error(respx_mock, ollama_client):
    respx_mock.post(f"{BASE_URL}/api/chat").mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )

    with pytest.raises(LLMError):
        ollama_client.messages.create(model="llama3.1", max_tokens=10, system="s", tools=[], messages=[])
