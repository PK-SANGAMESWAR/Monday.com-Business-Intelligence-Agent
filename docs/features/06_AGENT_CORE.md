# F06 — Agent Core: Tool Schemas, System Prompt, Tool-Use Loop

STATUS: **COMPLETE** — implemented and verified 2026-08-31. 32 new tests (20 in this
feature's own two files, plus the `board_repository` conftest fixture used by both). Full
suite `uv run pytest -q`: **356 passed**, 9 live deselected. `ANTHROPIC_API_KEY` is not
yet set in this environment — every test here runs against a stubbed client per plan
section 8, so nothing is blocked on it; live end-to-end chat is exercised once the key is
added (main.py already reports this readiness gap explicitly).

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)
Depends on: [04_NORMALIZATION_ANALYTICS.md](04_NORMALIZATION_ANALYTICS.md) (COMPLETE)

---

## 1. Objective

Wires F04/F05 up to the Anthropic Messages API as a tool-use loop: the model picks a tool
and arguments, `bi_agent/data`/`bi_agent/analytics` compute the answer, the model narrates
it. Per plan section 3.2, **the model never computes and never sees a raw row** — every
number it can mention arrives as a `MetricResult` it did not produce.

Scoped deliberately narrower than plan section 4.2's full tool list: `compare_boards`
(F07) and `leadership_brief` (F08) are not implemented here — those features come *after*
F06 in the dependency graph (plan section 7), and exposing a tool the agent cannot back
yet is worse than not exposing it. F06 ships `describe_data`, `query_deals`,
`query_work_orders`, `pipeline_health`, `revenue_and_collections`, `data_quality_report`.

## 2. Requirement mapping

| Req | Statement | How F06 satisfies it |
|---|---|---|
| **FR-10** | Interpret founder-level questions | System prompt + tool schemas give the model a structured surface to translate a question onto, rather than free-form reasoning about raw data. |
| **FR-11** | Ask clarifying questions when genuinely ambiguous | System prompt rule 4; tested with a stubbed LLM that returns no tool call and a question. |
| **FR-14** | Context and insight, not raw numbers | The model narrates `MetricResult` (value + coverage + caveats), never a bare figure. |
| **FR-15** | Conversational, multi-turn | `Agent` keeps `messages` across `ask()` calls within one instance. |
| **FR-16** | Graceful handling of API failures | Anthropic call failures wrapped as `LLMError` (F01); tool failures (`QuerySpecError`) returned to the model as a correctable JSON error, never raised to the user. |
| **NFR-2** | Determinism | Tool outputs are exactly F05's `MetricResult`s, serialized; no arithmetic happens in this layer or in the model. |

## 3. Technical design

### 3.1 Module layout

```text
bi_agent/agent/
  tools.py   TOOL_SCHEMAS (Anthropic tool-use JSON) + dispatch_tool()
  prompt.py  SYSTEM_PROMPT
  loop.py    Agent: the tool-use loop, conversation state, LLMError wrapping
```

### 3.2 Tool surface (this feature)

| Tool | Backed by |
|---|---|
| `describe_data(board)` | `schema.py` field table + observed coverage/sample values from `BoardRepository` |
| `query_deals(filters, group_by, metric, field)` | `QuerySpec(board="deals", ...)` -> `run_query` |
| `query_work_orders(...)` | Same, `board="work_orders"` |
| `pipeline_health(sector?, period?)` | `pipeline_value` + `stage_distribution` + `quality.stage_status_conflicts` |
| `revenue_and_collections(sector?, period?)` | `revenue_billed` + `collected_amount` + `receivable` |
| `data_quality_report(board)` | `DataQualityReport`, serialized |

A `QuerySpecError` raised inside a tool is caught by `dispatch_tool` and returned as
`{"error": ..., "hint": ...}` — a correctable tool result, per F01's design for that
exception, never an exception the user sees.

### 3.3 System prompt rules (`prompt.py`)

1. Never compute; every number comes from a tool call. Any `caveats` on a result must be
   stated, not dropped for brevity (CLAUDE.md's non-negotiable rule, made structural in
   F05 and enforced here by instruction rather than code — code cannot force the model to
   speak a sentence, only supply it one to speak).
2. "Revenue" defaults to billed value; deal value is pipeline, collected is cash (OQ-5).
   Ask when a question could mean any of the three.
3. Indian fiscal year (Apr-Mar). A period-fallback caveat (F05 section 3.7) must be
   surfaced verbatim, not silently absorbed.
4. Ask a clarifying question only when genuinely ambiguous.
5. Cross-board row joins are not available (no shared key, CLAUDE.md) and not yet a tool
   (F07) — say so plainly if asked, offer a per-board answer.
6. Call `describe_data` before guessing a field or category spelling.
7. On tool/API failure, say what failed and answer from what remains.

### 3.4 The loop (`loop.py`)

`Agent(repository, settings=None, *, client=None)`. `client` is the injection point for
tests (plan's established pattern: `MondayClient`'s `http_client`, `SeedWriter`'s
`http_client` — same shape here for the Anthropic SDK client). `ask(user_message) ->
AgentResponse(text, tool_calls)` runs up to `MAX_TOOL_ITERATIONS` rounds of
tool-call/tool-result before returning the model's final text, raising `LLMError` if the
Anthropic call itself fails or the loop never converges.

## 4. Files to create

| File | Responsibility |
|---|---|
| `bi_agent/agent/tools.py` | Tool schemas + dispatcher. |
| `bi_agent/agent/prompt.py` | System prompt. |
| `bi_agent/agent/loop.py` | `Agent`, `AgentResponse`. |
| `tests/unit/test_tools.py` | `dispatch_tool` against a repository built from the live fixtures; `QuerySpecError` -> tool-error shape. |
| `tests/unit/test_loop.py` | Stubbed Anthropic client: single-turn answer, multi-round tool use, clarifying question (no tool call), API failure -> `LLMError`, loop-limit failure. |

## 5. Test plan

| # | Case | Expectation |
|---|---|---|
| 98 | `describe_data("deals")` | Field list, always-null fields, sample values for a categorical. |
| 99 | `query_deals` valid spec | `MetricResult` dict with the F03/F05-verified deal-value sum. |
| 100 | `query_deals` invalid field | `{"error": ..., "hint": ...}`, not an exception. |
| 101 | Grouped query | `{"grouped": {...}}` shape. |
| 102 | `pipeline_health` | Composite dict incl. `stage_status_conflicts`. |
| 103 | `revenue_and_collections` | Three `MetricResult`s, `collected`'s caveat present. |
| 104 | `data_quality_report("work_orders")` | Always-null fields and casing fixes present. |
| 105 | Stubbed single-turn answer | No tool call -> final text returned directly. |
| 106 | Stubbed tool-call round | Tool result fed back; second round returns text. |
| 107 | Clarifying question | Model's first response is text with no tool call, asking a question - loop returns it, does not force a tool call. |
| 108 | Anthropic call raises | `LLMError`, conversation state preserved. |
| 109 | Loop never stops calling tools | `LLMError` after `MAX_TOOL_ITERATIONS`, not an infinite loop. |

## 6. Acceptance criteria

- Every tool's numeric output traces to a `MetricResult`; no arithmetic in `tools.py` or
  `loop.py`.
- A `QuerySpecError` never reaches the user as a crash.
- `Agent` is fully testable with a stubbed client — no `ANTHROPIC_API_KEY` needed offline.
- Full suite green.

## 7. Implementation results

Implemented as designed: `bi_agent/agent/{tools,prompt,loop}.py`. Added `anthropic` via
`uv add anthropic` (planned dependency, plan section 5).

A `board_repository` fixture was added to `tests/conftest.py` — the first fixture in the
suite that wires *both* boards behind one mocked transport, routing `BOARD_ITEMS_FIRST`
by requested board id to whichever live recording matches. Needed because F06 is the
first layer where a single test can plausibly touch both boards in one call
(`revenue_and_collections` reads Work Orders while `pipeline_health` reads Deals, and a
model conversation may call either tool in either order).

**Acceptance criteria, verified:**

| Criterion | Result |
|---|---|
| Every tool traces to a `MetricResult`; no arithmetic in `tools.py`/`loop.py` | PASS — every numeric tool output is `dataclasses.asdict(MetricResult)` |
| `QuerySpecError` never reaches the user as a crash | PASS — `test_query_deals_invalid_field_returns_correctable_error_not_a_crash`, plus a broadened catch for `KeyError`/`TypeError`/`ValueError` (malformed arguments, not just invalid specs) added during implementation |
| Fully testable with a stubbed client | PASS — 20 tests in `test_tools.py`/`test_loop.py`, zero network, zero API key |
| Full suite green | PASS — 356 passed, 0 failed; live suite (9) still green afterward |

No bugs found by the live/offline gap this time — everything here is offline-only by
design (there is no live LLM to test against without `ANTHROPIC_API_KEY`), so nothing was
left to catch except what the unit/stub tests already covered on the first run.

## 8. Known limitations

- **No live end-to-end chat exercised** — `ANTHROPIC_API_KEY` is not set in this
  environment (plan section 9.2, still outstanding). Everything here is proven against a
  stubbed Anthropic client; the real model's tool-selection behavior (does it actually
  call `describe_data` before guessing a field, does it actually ask a clarifying
  question when the prompt says to) is unverified until F09/F10's end-to-end pass with a
  real key.
- **`compare_boards` and `leadership_brief` are not exposed** (deliberate scope cut, see
  section 1) — the model cannot yet be asked a cross-board or leadership-brief question
  and will have to say so per system prompt rule 5, until F07/F08 land.
- **No conversation persistence** — `Agent` holds `self._messages` in memory only, gone
  when the process exits. F09 (Streamlit) will need session-state wiring; not a gap in
  this feature so much as a boundary of it.
- **`MAX_TOOL_ITERATIONS = 6` is a judgment call**, not a measured number — no real
  conversation has run against it yet to confirm 6 is enough headroom for a genuinely
  multi-step question without being so high that a stuck loop wastes many API calls
  before failing.
