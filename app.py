"""Streamlit chat entrypoint (F09).

Flat at the repo root, per the layout decision in `docs/00_IMPLEMENTATION_PLAN.md`
section 3.6: Streamlit Community Cloud runs `streamlit run app.py` from the repository
root with no install step, so nothing here may depend on the package being pip-installed.

Construction order matters and is deliberate: `BoardRepository` is a `st.cache_resource`
(one instance for the whole process — it already does its own TTL caching internally,
F04) but `Agent` lives in `st.session_state` (one per browser session) because its
conversation history must never leak between two founders chatting at once.
"""

from __future__ import annotations

import streamlit as st

from bi_agent.agent.loop import Agent, AgentResponse
from bi_agent.config import get_settings
from bi_agent.data.repository import BoardRepository
from bi_agent.errors import BIAgentError, ConfigError
from bi_agent.logging_config import configure_logging
from bi_agent.monday.boards import BoardReader
from bi_agent.monday.client import MondayClient

st.set_page_config(page_title="monday.com BI Agent", page_icon="\U0001f4ca", layout="wide")


@st.cache_resource
def _build_repository() -> BoardRepository:
    settings = get_settings()
    client = MondayClient(settings)
    reader = BoardReader(client)
    return BoardRepository(reader)


def _get_agent(repository: BoardRepository) -> Agent | None:
    """Build (once) and return this session's `Agent`, or `None` if it could not be
    built. Tests exercise the happy path by monkeypatching `anthropic.Anthropic` -
    the same seam `Agent._build_client` already uses - rather than threading a
    fake client through here, since production code never needs to inject one.
    """
    if "agent" not in st.session_state:
        try:
            st.session_state["agent"] = Agent(repository, get_settings())
            st.session_state["agent_error"] = None
        except BIAgentError as exc:
            st.session_state["agent"] = None
            st.session_state["agent_error"] = exc.user_message
    return st.session_state["agent"]


def _render_data_quality_panel(repository: BoardRepository) -> None:
    with st.sidebar.expander("Data quality", expanded=False):
        for board, label in (("deals", "Deals"), ("work_orders", "Work Orders")):
            data = repository.deals() if board == "deals" else repository.work_orders()
            quality = data.quality
            st.markdown(
                f"**{label}** — {quality.n_total_rows} rows "
                f"({quality.n_junk_rows_excluded} junk rows excluded)"
            )
            always_null = quality.always_null_fields()
            if always_null:
                st.caption("Always empty: " + ", ".join(always_null))
            if quality.stage_status_conflicts:
                st.caption(
                    f"{quality.stage_status_conflicts} rows where Deal Status "
                    "contradicts Deal Stage"
                )


def _render_tool_calls(tool_calls: list[dict]) -> None:
    if not tool_calls:
        return
    with st.expander("Data behind this answer"):
        for call in tool_calls:
            st.markdown(f"`{call['name']}`")
            st.json(call["result"])


def _render_history() -> None:
    for turn in st.session_state["history"]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["text"])
            _render_tool_calls(turn.get("tool_calls", []))


def main() -> None:
    try:
        settings = get_settings()
    except ConfigError as exc:
        st.error("Configuration error")
        st.write(exc.user_message)
        st.stop()
        return

    configure_logging(settings.log_level, secrets=settings.secret_values())
    repository = _build_repository()

    st.title("\U0001f4ca monday.com Business Intelligence Agent")
    st.caption(
        "Ask about pipeline, revenue, collections, or a leadership update. Every "
        "figure is computed from live board data, never invented."
    )

    with st.sidebar:
        st.header("Session")
        if st.button("Refresh board data"):
            repository.invalidate()
            st.success("Cache cleared. The next question re-fetches from monday.com.")
        st.caption(f"Cache TTL: {settings.cache_ttl_seconds}s")

    _render_data_quality_panel(repository)

    if "history" not in st.session_state:
        st.session_state["history"] = []

    agent = _get_agent(repository)
    if agent is None:
        st.warning(
            st.session_state.get("agent_error")
            or "The reasoning service is not configured."
        )

    _render_history()

    question = st.chat_input(
        "Ask a question about your pipeline or work orders...",
        disabled=agent is None,
    )
    if not question:
        return

    st.session_state["history"].append({"role": "user", "text": question, "tool_calls": []})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                response: AgentResponse = agent.ask(question)
                text, tool_calls = response.text, response.tool_calls
            except BIAgentError as exc:
                text, tool_calls = exc.user_message, []
        st.markdown(text)
        _render_tool_calls(tool_calls)

    st.session_state["history"].append(
        {"role": "assistant", "text": text, "tool_calls": tool_calls}
    )


if __name__ == "__main__":
    main()
