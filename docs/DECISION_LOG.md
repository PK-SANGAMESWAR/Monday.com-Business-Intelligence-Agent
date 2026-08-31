# Decision Log

*(≤2 pages, per the brief's Decision Log deliverable.)*

## Key assumptions

- **"This quarter" = Indian fiscal year (April–March), anchored to the real clock.**
  The workbook data ends 2026-01 but the system clock is 2026-08-31; a calendar-quarter
  reading would land "this quarter" on a period with zero rows. Fiscal-quarter framing
  was the closer fit to "founder asking about the business," and when the resolved period
  genuinely has no rows, the agent says so and offers the most recent period that does —
  it never silently substitutes a different window.
- **"Revenue" defaults to billed value**, not pipeline (deal value) or cash collected.
  The three are named explicitly in every answer that touches money, and the agent asks
  rather than guesses when a question could mean any of them — CLAUDE.md is explicit that
  blurring these three is a specific failure mode to avoid.
- **The sample workbooks are schema reference and seed data only**, never a runtime data
  source. The agent fetches monday.com live on every session (subject to the TTL cache);
  hardcoding the xlsx would satisfy the demo but violate the stated integration
  requirement outright.
- **A monday.com personal access token carries full account permissions** — there is no
  read-only personal token tier. Read-only is therefore enforced in our own client (any
  GraphQL document containing a `mutation` operation is rejected before the request
  leaves the process), not at the credential layer. This is a guarantee about our code,
  not about what the token itself could do if a bug in our code asked it to.

## Trade-offs chosen, and why

| Decision | Alternative considered | Why this one |
|----------|------------------------|---------------|
| **The LLM picks a tool; tested Python computes.** No code execution, no path for the model to do its own arithmetic. | Let the model write pandas/SQL against the data directly | Arbitrary code execution in a hosted app is a real risk, output would stop being deterministic, and every messiness rule (junk rows, zero-as-missing, stage/status conflict) would have to be correctly re-derived by the model on every single question instead of being proven once in a test. |
| **Named tools + one validated query spec** (`filters` + `group_by` + `metric`, schema-checked before execution), not a fixed metric-per-question set. | A closed set of hand-written metric functions only | The closed set is safer but brittle — every question the author didn't anticipate becomes "I can't answer that." The validated spec is exactly as safe (invalid specs are a correctable tool error, never reach a user) but covers unanticipated questions. |
| **Fetch-once per board, cache with a TTL, compute locally**, instead of translating each question into a filtered API call. | Query monday.com per-question with server-side filters | ~500 rows total fits comfortably in memory; fetching whole boards means data-quality coverage stats ("165 of 344") are true board-wide rather than an artifact of whichever page a filtered query happened to touch. Recorded explicitly as a decision that stops being right somewhere around 10k rows/board. |
| **Refuse row-level joins between Deals and Work Orders**; compare only on `sector` and `owner_code`, the two dimensions genuinely shared under the same name. | Join on `Deal Name` | 346 deal rows carry only 155 distinct names (`Sakura` alone is 27 rows); a name join is many-to-many and would silently multiply pipeline/revenue. A wrong number that looks confident is worse than an explicit "I can't join these." |
| **Direct GraphQL API v2 over `httpx`**, not the monday.com MCP server. | MCP server | MCP would add a second (Node) process to a hosted single-process Streamlit deployment and its tool surface is tuned for mutation, which this project must never do. Direct GraphQL gives explicit, testable control over pagination, retry/backoff, and the read-only gate. |
| **Streamlit, not FastAPI + a separate frontend.** | React/Next.js frontend + API backend | Chat UI, streaming, and session state in one small file; a two-service split would triple the surface area for no graded benefit at this scope. Trade-off accepted: less control over UI polish than a custom frontend would give. |
| **pandas over DuckDB/SQL**, given the row counts involved. | An in-process SQL engine | 176/346-row boards don't need a query engine; pandas groupby/null-handling is the right-sized tool, and it is what the rest of the Python stack already assumes. |
| **A pluggable LLM backend (`Settings.llm_provider`) with an Ollama adapter alongside Anthropic**, rather than hard-requiring an Anthropic key to run at all. | Block on obtaining an Anthropic key before any end-to-end testing | `bi_agent/agent/ollama_client.py` implements the same `.messages.create(...)` shape `loop.py` already calls, so `loop.py`, `tools.py`, and every analytics module are unaware which backend answered — switching back to Anthropic is `LLM_PROVIDER=anthropic`, no code change. Verified end-to-end against the real seeded boards with a local `qwen2.5:7b` (tool-calling capable); the local model needs its own longer timeout (`OLLAMA_TIMEOUT_SECONDS`, default 120s vs. monday.com's 30s) since a cold local model's first response is much slower than a hosted API's. |

## What we'd do differently with more time

- **Register a monday.com OAuth app scoped to `boards:read`** and close the read-only gap
  at the credential layer instead of only in our own client code — the honest remaining
  gap in FR-5's guarantee.
- **Raise `analytics/metrics.py` branch coverage from 80% to the 90% target** — the
  missing branches are metric-argument error paths (unknown field, wrong metric for a
  categorical), not core logic, but they're still untested code.
- **A small deterministic-arithmetic fuzzer**: generate random valid `QuerySpec`s, compare
  against an independent pandas computation, to catch a normalization/aggregation
  regression that hand-picked golden values happen not to exercise.
- **Persist conversation history** (currently per-`st.session_state`, lost on refresh) if
  this were to become a real internal tool rather than a prototype.
- **A registered app with a scoped token**, replacing the current personal-access-token
  setup, before this ever touched non-sample data.

## How "leadership updates" was interpreted (optional requirement)

The brief leaves the shape of "help prepare data for leadership updates" undefined. This
was read as: **compose metrics already proven correct into one structured brief with zero
new arithmetic**, exposed as a `leadership_brief` tool the agent can call conversationally
("give me a leadership update on pipeline") and that also produces a ready-to-paste
Markdown document (`bi_agent/analytics/briefing.py`). It reports pipeline value, billed
revenue, collected cash, outstanding receivable, deal-stage distribution, the top sectors
by pipeline, the count of stage/status conflicts, and every data-quality caveat that
applies — because a leadership update built on unstated caveats is the specific failure
mode this whole project is meant to avoid. This module computes nothing itself; every
figure in it is a `MetricResult` `bi_agent/analytics/metrics.py` already returned and
already tested independently.
