# F09 — Streamlit Chat UI

STATUS: **COMPLETE** — implemented and verified 2026-08-31. 7 new tests
(`tests/integration/test_app.py`, `streamlit.testing.v1.AppTest`). Full suite
`uv run pytest -q`: **381 passed**, 10 live deselected. Manually started
`uv run streamlit run app.py` against this environment's real `.env`
(`MONDAY_API_KEY` set, `ANTHROPIC_API_KEY` absent) — served HTTP 200, `/_stcore/health`
reported `ok`, no exception in the server log.

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)
Depends on: [06_AGENT_CORE.md](06_AGENT_CORE.md), [07_CROSSBOARD.md](07_CROSSBOARD.md),
[08_LEADERSHIP_BRIEF.md](08_LEADERSHIP_BRIEF.md) (all COMPLETE — every tool `Agent` can
call, including `compare_boards` and `leadership_brief`, already exists)

---

## 1. Objective

The last layer before deployment (plan section 7: `F06 -> F09 -> F10`): a hosted chat
surface over `Agent` (F06). Per plan section 3.6, this is `app.py` at the repo root (flat
layout, not `src/`, so Streamlit Community Cloud can run it with no install step).

Scope, deliberately narrow: wire `Settings -> MondayClient -> BoardReader ->
BoardRepository -> Agent` once per process, hold one `Agent` per browser session in
`st.session_state` (F06's own "Known limitations" note that conversation state needs
session-state wiring here), render the chat, and **degrade visibly rather than crash**
when a credential is missing — this environment currently has `MONDAY_API_KEY` but not
`ANTHROPIC_API_KEY` (plan section 9.2), so the missing-Anthropic-key path is not a
hypothetical edge case here, it is today's actual state and must be demonstrably handled,
not just asserted in a docstring.

## 2. Requirement mapping

| Req | Statement | How F09 satisfies it |
|---|---|---|
| **FR-15** | Conversational interface with multi-turn context | `st.chat_input`/`st.chat_message` over one `Agent` instance kept in `st.session_state` for the life of the browser session. |
| **DL-1** | Hosted prototype, testable with no local setup | `app.py` is the Streamlit Community Cloud entrypoint (plan section 3.5's hosting choice); F10 does the actual deploy. |
| **FR-9 / FR-14** | Surface caveats; insight plus number | The model's narrated text already carries caveats (prompt rule 1); the UI additionally renders an expander per answer showing the raw tool call(s) and their `MetricResult` JSON (`n_used`, `n_total`, `caveats`) so a founder can verify the number behind the sentence, not just trust the prose. |
| **FR-16 / NFR-7** | Graceful degradation; observability | `ConfigError` (no `MONDAY_API_KEY`) stops the page with a setup message before any board call; a missing/failing `ANTHROPIC_API_KEY` (`LLMError` from `Agent.__init__`) disables chat input with a visible banner but leaves the data-quality sidebar working, since that only needs the repository, not the model. `configure_logging` is called every rerun (its docstring already anticipates this — Streamlit re-executes the whole script per interaction). |
| Plan §3.6 | `app.py` at repo root | New file; nothing under `bi_agent/` changes. |

## 3. Technical design

### 3.1 Construction order

```text
get_settings()              # ConfigError -> st.error + st.stop(), before anything else
  -> MondayClient(settings)
  -> BoardReader(client)
  -> BoardRepository(reader)   # cached process-wide via st.cache_resource — one fetch
                                 # per cache window across all users, per plan section 3.4
  -> Agent(repository, settings)  # per browser session, st.session_state — NOT cached
                                    # as a resource, because messages must not leak
                                    # between users' conversations
```

`BoardRepository` is `st.cache_resource`-cached (keyed on nothing — one instance for the
process) because it already does its own TTL caching internally (F04); Streamlit does not
need to re-run its constructor on every rerun, only reuse the same object so the TTL
means what it says. `Agent` is intentionally **not** shared this way: two founders
chatting at once must not see each other's conversation history.

### 3.2 Degradation paths exercised by this feature

| Failure | Where caught | User sees | Sidebar (data quality) |
|---|---|---|---|
| `MONDAY_API_KEY` missing/invalid (`ConfigError`) | Top of `main()`, before any board call | `st.error` with `exc.user_message`, page stops | N/A — nothing can run without a repository |
| `ANTHROPIC_API_KEY` missing (`LLMError` from `Agent.__init__`, current state of this environment) | Agent construction, session-state | `st.warning` banner; `st.chat_input(disabled=True)` | **Still works** — repository-only |
| `agent.ask()` raises `BIAgentError` mid-conversation | Around the `ask()` call | The exception's `user_message` rendered as the assistant's turn, conversation continues | Unaffected |

No new exception types; F01's hierarchy already names every case (`errors.py`).

### 3.3 Session state

| Key | Lifetime | Holds |
|---|---|---|
| `st.session_state["agent"]` | Per browser session | `Agent` instance, or `None` if construction failed |
| `st.session_state["agent_error"]` | Per browser session | The `user_message` from the failed construction, or `None` |
| `st.session_state["history"]` | Per browser session | `list[{"role", "text", "tool_calls"}]` — separate from `Agent._messages` (F06's Anthropic-shaped message list) because the UI needs a render-friendly shape and must survive being read on every rerun without re-parsing SDK content blocks |

### 3.4 What "streaming" means here, honestly

Plan section 3.5 lists "streaming" as a Streamlit capability in the tech-stack table, but
`Agent.ask()` (F06) is request/response — it calls `messages.create()` once per tool round
and returns a complete `AgentResponse`, not a token stream. Implementing real token
streaming would mean changing F06's `Agent` API, which is out of scope for a UI feature.
F09 instead streams **at the turn level**: each user/assistant turn appears in the chat
transcript as soon as it completes, with a spinner during `agent.ask()`. Recorded here so
it is a stated scope decision, not a silently unmet claim.

### 3.5 Data-quality panel

A sidebar expander calls `repository.deals()` / `repository.work_orders()` directly (not
through the model) and renders `DataQualityReport.always_null_fields()`,
`stage_status_conflicts`, and row/junk counts — the same facts `data_quality_report`
exposes to the model, shown to the founder without needing to ask. A "Refresh board data"
button calls `repository.invalidate()`, forcing the next tool call to re-fetch rather than
serve the cached frame.

## 4. Files to create / modify

| File | Responsibility |
|---|---|
| `app.py` | Streamlit entrypoint: wiring, chat loop, sidebar, degradation. |
| `pyproject.toml` | Add `streamlit` (`uv add streamlit`). |
| `tests/integration/test_app.py` | `streamlit.testing.v1.AppTest` driving `app.py` in-process against the mocked transport (`board_repository`'s fixture pattern) and a stubbed Anthropic client. |
| `docs/features/09_STREAMLIT_UI.md` | This document. |

## 5. Implementation plan

1. `uv add streamlit`.
2. Write `app.py`: settings/repository/agent construction with the degradation table
   above, sidebar (refresh button + data-quality expander), chat loop rendering
   `st.session_state["history"]`, tool-call expander per answer.
3. Write `tests/integration/test_app.py` using `AppTest.from_file`, reusing the existing
   `respx`/fixture pattern from `tests/conftest.py` so no real network call is possible,
   and a `FakeAnthropicClient` monkeypatched onto `anthropic.Anthropic` (same message
   shapes `test_loop.py` already uses) for the happy-path conversation test.
4. Run the full suite; fix; record results below.
5. Manually run `uv run streamlit run app.py` and drive it in a browser to confirm the
   real degraded state (no Anthropic key in this environment) matches the plan.

## 6. Test plan

| # | Case | Expectation |
|---|---|---|
| 1 | App loads with valid `MONDAY_API_KEY`, no `ANTHROPIC_API_KEY` | Title renders; a warning banner about the reasoning service names what to set; `chat_input` is disabled; no exception. |
| 2 | Data-quality sidebar, no Anthropic key | Still renders both boards' row counts, junk-row counts, and always-null fields — proves the panel does not depend on `Agent`. |
| 3 | `MONDAY_API_KEY` missing (`ConfigError`) | Page shows the config error message and does not attempt any board call (no request recorded on the mocked route). |
| 4 | Happy path: fake Anthropic key + stubbed client, single-turn text answer | Asking a question renders the user bubble, then the assistant's text; no tool-call expander when there were no tool calls. |
| 5 | Happy path with a tool call | Assistant bubble plus an expander containing the tool name and its `MetricResult` JSON (`n_used`/`n_total`/`caveats` visible). |
| 6 | `agent.ask()` raises `BIAgentError` mid-conversation | The turn renders the exception's `user_message`, not a traceback; the app does not crash; a second question still works. |
| 7 | "Refresh board data" button | Clicking it calls `repository.invalidate()` (asserted via a spy/cache-entry check) and shows a confirmation. |
| 8 | Two turns in one session | Second question's rendered history includes both prior turns — proves `st.session_state["history"]` persists across reruns within `AppTest`. |

## 7. Acceptance criteria

- `app.py` never raises an unhandled exception for any of the failures in section 3.2 —
  each renders a specific, non-generic message.
- The data-quality sidebar works with no `ANTHROPIC_API_KEY` set (proves it does not
  depend on `Agent`).
- No test in `test_app.py` performs a real HTTP call or reaches a real LLM.
- Full suite green, offline, no API key required to run it.

## 8. Implementation results

Implemented as designed: `app.py` at the repo root, `streamlit` added via `uv add
streamlit` (1.62.0).

**Deviation from the original design (section 3.1):** the draft plan considered passing
a `client` injection parameter through `_get_agent` for testability, mirroring
`Agent.__init__`'s own `client=` seam. Dropped during implementation because `main()`
never needed it — production code always builds a real `Agent`. Tests instead
monkeypatch `anthropic.Anthropic` directly (the same import `Agent._build_client` already
does lazily), which exercises the *real* construction path instead of a parallel
test-only one. Threading an unused parameter through `_get_agent` would have been
exactly the kind of speculative abstraction the project's code-quality rules rule out.

**A real bug found by `AppTest`, not by inspection:** the first stubbed-conversation test
(`FakeAnthropicMessages` built with `iter(responses)` captured at `Agent` construction
time) passed for a single turn but broke on a second question in the same session —
`Agent` is built once per `st.session_state` (by design, section 3.1), so a fresh
per-call iterator can never see responses a test queues *after* that construction. Fixed
by having `FakeAnthropicMessages` pop from a list object shared by reference with
`FakeAnthropicClient`, so queuing more responses mid-conversation reaches the same
`Agent` instance's stub. Recorded because it is a general lesson for testing anything
built once per session and called multiple times, not an app.py-specific quirk.

**`st.cache_resource` test isolation:** `_build_repository` is deliberately
process-wide-cached (section 3.1), which means it is *shared across tests* in the same
pytest process unless explicitly cleared — the first version of `test_app.py` had no
such fixture and later tests silently reused an earlier test's `BoardRepository` (bound
to a `respx_mock` context that had already exited). Fixed with an autouse fixture calling
`st.cache_resource.clear()` before and after every test.

**Acceptance criteria, verified:**

| Criterion | Result |
|---|---|
| No unhandled exception for any failure in section 3.2 | PASS — `test_config_error_stops_before_any_board_call`, `test_missing_anthropic_key_disables_chat_but_not_data_quality`, `test_tool_use_loop_error_is_shown_as_the_assistant_turn_not_a_crash` |
| Data-quality sidebar works with no `ANTHROPIC_API_KEY` | PASS — `test_missing_anthropic_key_disables_chat_but_not_data_quality` asserts both boards' row counts render while `chat_input` stays disabled |
| No test performs a real HTTP call or reaches a real LLM | PASS — `respx_mock` intercepts the monday.com transport (asserted via `route.calls.call_count`); `anthropic.Anthropic` is monkeypatched to `FakeAnthropicClient` |
| Full suite green, offline, no API key | PASS — 381 passed, 0 failed |

## 9. Known limitations

- **No real end-to-end chat exercised** — `ANTHROPIC_API_KEY` is still not set in this
  environment (plan section 9.2, carried over from F06/F07/F08). The real model's
  behaviour inside the actual Streamlit widget tree (tool selection, clarifying
  questions) is proven only against the stub, same boundary F06 already documented.
- **Turn-level, not token-level, streaming** (section 3.4) — a deliberate scope decision
  given `Agent.ask()`'s request/response shape; changing that would be an F06 change, not
  a UI one.
- **No automated real-browser check** — verification here is `AppTest` (in-process,
  exercises the actual script and widget tree) plus a manual server boot confirming
  HTTP 200 and a clean `/_stcore/health`. Nobody has clicked through this UI in an actual
  browser tab yet; that is F10's live end-to-end pass, once `ANTHROPIC_API_KEY` exists.
- **`st.cache_resource` is process-wide, not per-deployment-instance-aware** — fine for a
  single Streamlit Community Cloud instance (the deployment target), but means every
  visitor shares one `BoardRepository` and its TTL cache; this is the intended behaviour
  (plan section 3.4's "one fetch per cache window" is meant to be global, not
  per-session), stated here so it reads as a decision rather than an oversight.
- **The "Refresh board data" button clears the cache but does not pre-warm it** — the
  next question (from any user) pays the re-fetch cost; acceptable at this data size
  (NFR-1's <10s budget) but worth knowing before assuming the click itself is free.
