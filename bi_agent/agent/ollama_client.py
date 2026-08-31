"""Ollama adapter: same `.messages.create(...)` surface `loop.py` expects from
the Anthropic SDK, backed by a local Ollama daemon instead.

Exists so the agent can be evaluated end-to-end before an `ANTHROPIC_API_KEY`
exists - `LLM_PROVIDER=ollama` is the only setting that changes; `loop.py`,
`tools.py`, and every analytics module are untouched, because they never see
which backend answered. Swapping back to Anthropic later is the same
one-variable change in reverse.

Two shape differences from the Anthropic SDK this module bridges:

1. Ollama's tool calls carry no id, so one is synthesized per call
   (`ollama_call_<n>`) to stand in for the `tool_use_id` `loop.py` pairs
   against a later `tool_result`.
2. Anthropic returns a tool-execution turn as one user message holding a list
   of `tool_result` blocks; Ollama expects each result as its own
   `role: "tool"` message.

Uses `httpx` (already a project dependency) against Ollama's native
`/api/chat` endpoint rather than pulling in an OpenAI-compatible SDK for a
single call site.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx

from bi_agent.errors import LLMError

__all__ = ["OllamaClient"]

logger = logging.getLogger(__name__)

#: Ollama's own default when run locally with `ollama serve`.
DEFAULT_BASE_URL = "http://localhost:11434"

#: How much of a failing response body reaches the exception message.
BODY_LOG_LIMIT = 500


def _to_ollama_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic's `{name, description, input_schema}` -> Ollama's function-tool shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": schema["input_schema"],
            },
        }
        for schema in tool_schemas
    ]


def _tool_result_messages(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"role": "tool", "content": block["content"]} for block in content]


def _assistant_turn_message(role: str, content: list[Any]) -> dict[str, Any]:
    """A previously-returned turn (our own `SimpleNamespace` blocks), replayed back."""
    text_parts = [block.text for block in content if getattr(block, "type", None) == "text"]
    tool_calls = [
        {"function": {"name": block.name, "arguments": block.input}}
        for block in content
        if getattr(block, "type", None) == "tool_use"
    ]
    message: dict[str, Any] = {"role": role, "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _to_ollama_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The growing Anthropic-shaped conversation -> Ollama's chat message list."""
    converted: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
        elif content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
            converted.extend(_tool_result_messages(content))
        else:
            converted.append(_assistant_turn_message(role, content))
    return converted


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ollama tool call arguments were not valid JSON: %r", raw[:BODY_LOG_LIMIT])
    return {}


def _parse_response(payload: dict[str, Any]) -> SimpleNamespace:
    """Ollama's `{"message": {...}}` -> the block list `loop.py` iterates over."""
    message = payload.get("message") or {}
    blocks: list[SimpleNamespace] = []

    text = message.get("content")
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))

    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function", {})
        blocks.append(
            SimpleNamespace(
                type="tool_use",
                id=f"ollama_call_{index}",
                name=function.get("name", ""),
                input=_parse_arguments(function.get("arguments", {})),
            )
        )

    if not blocks:
        # Neither text nor a tool call: still a turn loop.py must be able to
        # finish `ask()` on, not crash trying to join an empty block list.
        blocks.append(SimpleNamespace(type="text", text=""))

    return SimpleNamespace(content=blocks)


class _Messages:
    """Stands in for the Anthropic SDK's `client.messages` namespace."""

    def __init__(self, client: OllamaClient) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        max_tokens: int,  # unused: Ollama has no equivalent knob; kept for signature parity
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> SimpleNamespace:
        return self._client._chat(model=model, system=system, tools=tools, messages=messages)


class OllamaClient:
    """Talks to a local Ollama daemon; matches enough of the Anthropic SDK's
    surface (`.messages.create(...)`) that `Agent` cannot tell the two apart."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.messages = _Messages(self)
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout)
        )

    def close(self) -> None:
        if self._owns_client and not self._http.is_closed:
            self._http.close()

    def _chat(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> SimpleNamespace:
        payload = {
            "model": model,
            "messages": _to_ollama_messages(system, messages),
            "tools": _to_ollama_tools(tools),
            "stream": False,
        }

        try:
            response = self._http.post("/api/chat", json=payload)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise LLMError(
                f"could not reach Ollama at {self._http.base_url}: {exc}",
                user_message=(
                    "I could not reach the local Ollama server. Make sure "
                    "`ollama serve` is running and OLLAMA_BASE_URL is correct."
                ),
            ) from exc
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:BODY_LOG_LIMIT]
            raise LLMError(
                f"Ollama returned HTTP {exc.response.status_code} for model {model!r}: {body}",
                user_message=(
                    f"The local Ollama server rejected the request (is model "
                    f"{model!r} pulled? try `ollama pull {model}`)."
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(
                f"Ollama returned a non-JSON response: {response.text[:BODY_LOG_LIMIT]}"
            ) from exc

        return _parse_response(body)
