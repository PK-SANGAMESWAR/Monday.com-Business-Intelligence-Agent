"""The tool-use loop: conversation state, tool dispatch, degradation.

`client` is the same injection point F02's `MondayClient` and F03's `SeedWriter` use for
their HTTP transports (`http_client=...`) - here it is either the Anthropic SDK client or
`bi_agent.agent.ollama_client.OllamaClient` (selected by `Settings.llm_provider`), both of
which expose the same `.messages.create(...)` shape. That lets `Agent` be fully testable
with a stub that returns canned `messages.create` responses and never touches the network
or a real API key (plan section 8's "stubbed LLM" tests), and lets the agent run against a
local model before an ANTHROPIC_API_KEY exists.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from bi_agent.agent.prompt import SYSTEM_PROMPT
from bi_agent.agent.tools import TOOL_SCHEMAS, dispatch_tool
from bi_agent.config import Settings, get_settings
from bi_agent.data.repository import BoardRepository
from bi_agent.errors import LLMError

__all__ = ["Agent", "AgentResponse", "MAX_TOOL_ITERATIONS"]

logger = logging.getLogger(__name__)

#: A bound on tool-call rounds within a single `ask()`. Not a normal path - it exists so
#: a model stuck calling tools forever degrades to a named error instead of hanging the
#: process (the same reasoning as `boards.py`'s `MAX_PAGES`).
MAX_TOOL_ITERATIONS = 6

#: max_tokens for each Messages API call. Generous enough for a narrated answer with
#: caveats; not a knob anything here needs to tune per-question.
MAX_RESPONSE_TOKENS = 1024


class _AnthropicLike(Protocol):
    messages: Any


@dataclass
class AgentResponse:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    """One conversation. Construct a new `Agent` per chat session."""

    def __init__(
        self,
        repository: BoardRepository,
        settings: Settings | None = None,
        *,
        client: _AnthropicLike | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository
        self._client = client or self._build_client()
        self._messages: list[dict[str, Any]] = []

    def _build_client(self) -> _AnthropicLike:
        if self.settings.llm_provider == "ollama":
            from bi_agent.agent.ollama_client import OllamaClient

            return OllamaClient(
                base_url=self.settings.ollama_base_url,
                timeout=self.settings.ollama_timeout_seconds,
            )

        if not self.settings.has_anthropic_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set",
                user_message=(
                    "The reasoning service is not configured, so I cannot answer yet. "
                    "Set ANTHROPIC_API_KEY and restart, or set LLM_PROVIDER=ollama to "
                    "use a local model instead."
                ),
            )
        import anthropic

        return anthropic.Anthropic(
            api_key=self.settings.anthropic_api_key.get_secret_value()
        )

    def ask(self, user_message: str) -> AgentResponse:
        """Send one user turn, run any tool calls the model makes, return its answer.

        Raises `LLMError` on an Anthropic API failure or an unconverged tool loop. The
        conversation (`self._messages`) is preserved either way, so a retried `ask()`
        does not lose prior turns - the failure is the *latest* turn's problem, not the
        conversation's.
        """
        self._messages.append({"role": "user", "content": user_message})
        tool_calls_made: list[dict[str, Any]] = []

        for _ in range(MAX_TOOL_ITERATIONS):
            response = self._call_model()
            self._messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [
                block for block in response.content if getattr(block, "type", None) == "tool_use"
            ]
            if not tool_use_blocks:
                text = "".join(
                    block.text for block in response.content if getattr(block, "type", None) == "text"
                )
                return AgentResponse(text=text, tool_calls=tool_calls_made)

            tool_results = []
            for block in tool_use_blocks:
                result = dispatch_tool(block.name, block.input, repository=self.repository)
                tool_calls_made.append({"name": block.name, "input": block.input, "result": result})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )
            self._messages.append({"role": "user", "content": tool_results})

        raise LLMError(
            f"tool-use loop did not converge within {MAX_TOOL_ITERATIONS} rounds",
            user_message=(
                "I made too many tool calls trying to answer that without reaching a "
                "conclusion. Please try rephrasing the question."
            ),
        )

    def _call_model(self) -> Any:
        try:
            return self._client.messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                messages=self._messages,
            )
        except LLMError:
            raise
        except Exception as exc:  # the Anthropic SDK's / Ollama adapter's own exceptions
            logger.error("LLM API call failed: %s", exc)
            raise LLMError(f"LLM API call failed: {exc}") from exc
