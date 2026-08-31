# 00 — Master Implementation Plan

STATUS: **Planned** — architecture decisions resolved (§9.1). Awaiting approval for Feature 01.
No application code written yet.

Source of truth: [PROBLEM-STATEMENT.md](../PROBLEM-STATEMENT.md).
Measured data evidence: [DATA_PROFILE.md](DATA_PROFILE.md).

---

## 1. Overview and problem understanding

A founder asks a question in plain English — *"How's our pipeline looking for energy this
quarter?"* — and today someone has to pull two monday.com boards by hand, clean them,
reconcile them, and write up an answer.

We are building the agent that does that. Its job is not "run a query"; it is to
**interpret an imprecise business question, fetch live board data, clean known-messy
records deterministically, compute the right number, and hand back that number together
with what it means and what it cannot be trusted for.**

The hardest requirement in this brief is not the LLM and not the API. It is
**honesty under bad data**. 52% of deals have no value, four work-order columns are 100%
empty, `Deal Status` contradicts `Deal Stage` for 70 records, and the two boards cannot be
joined. An agent that confidently answers "your pipeline is ₹2.31B" is wrong. The correct
answer is "₹2.31B across the 165 of 344 deals that carry a value — the other 179 are
unpriced, so treat this as a floor."

That single behaviour drives the whole architecture below.

## 2. Requirements breakdown

### 2.1 Functional (from the problem statement)

| ID | Requirement | Source | Feature |
|----|-------------|--------|---------|
| FR-1 | Connect to monday.com via MCP or API | Core Features 1 | F02 |
| FR-2 | Handle authentication and connection management | Core Features 1 | F01, F02 |
| FR-3 | Read all data from both boards | Integration Reqs | F02 |
| FR-4 | Never hardcode CSV data; query monday.com dynamically at runtime | Integration Reqs | F02 |
| FR-5 | Read-only — no mutations to boards, items, or columns | Integration Reqs | F02 |
| FR-6 | Handle missing/null values gracefully | Core Features 2 | F04 |
| FR-7 | Normalize inconsistent dates, naming conventions, text fields | Core Features 2 | F04 |
| FR-8 | Produce meaningful results from incomplete data | Core Features 2 | F04, F05 |
| FR-9 | Communicate data-quality issues and caveats to the user | Core Features 2 | F04, F05, F06 |
| FR-10 | Interpret founder-level business questions | Core Features 3 | F06 |
| FR-11 | Ask clarifying questions when a query is genuinely ambiguous | Core Features 3 | F06 |
| FR-12 | Answer on revenue, pipeline health, sectoral performance, operational metrics | Core Features 4 | F05 |
| FR-13 | Query across both boards when needed | Core Features 4 | F07 |
| FR-14 | Provide context and insight, not just raw numbers | Core Features 4 | F05, F06 |
| FR-15 | Conversational interface with multi-turn context | Tech Expectations | F06, F09 |
| FR-16 | Graceful handling of API failures | Tech Expectations | F02, F06 |
| FR-17 | *(Optional)* Help prepare data for leadership updates | Additional Req | F08 |

### 2.2 Deliverables

| ID | Deliverable | Feature |
|----|-------------|---------|
| DL-1 | Hosted prototype, link-accessible, testable with no local setup | F10 |
| DL-2 | Decision Log, 2 pages max — assumptions, trade-offs, what we would do differently, how "leadership updates" was interpreted | F10 |
| DL-3 | Source code + README with architecture and monday.com setup instructions | F10 |
| DL-4 | monday.com boards created from the two sample workbooks with sensible column types | F03 |

### 2.3 Non-functional

| ID | Requirement | Target / approach |
|----|-------------|-------------------|
| NFR-1 | Answer latency | < 10 s typical. Boards are 344 + 176 rows — fetch once, cache, compute in-process. |
| NFR-2 | Determinism of numbers | Every figure comes from tested Python, never from the model's arithmetic. |
| NFR-3 | Reproducible tests | Full suite runs offline against recorded monday.com fixtures; no network, no API key. |
| NFR-4 | Secret handling | Tokens from environment only. `.env` gitignored, `.env.example` committed. No secret in code, logs, or error text. |
| NFR-5 | Read-only safety | Mutation-capable GraphQL operations are rejected before the request leaves the client. |
| NFR-6 | Cost | One board fetch per cache window, not per question. |
| NFR-7 | Observability | Structured logs for every API call, tool call, and coercion failure. |
| NFR-8 | Maintainability | Board schema expressed as data (a mapping), not scattered conditionals. |

### 2.4 Implicit requirements not stated in the brief

- **Board seeding is a prerequisite deliverable.** The brief says "import these into
  monday.com" but the boards do not exist yet. Without them there is nothing to query, so
  seeding is Feature 03 — scripted and repeatable rather than done by hand.
- **Column-ID indirection.** monday.com identifies columns by opaque IDs
  (`text_mkq1abc`), not by title. The agent must resolve title to ID at runtime, or it
  breaks the moment a board is recreated.
- **Cursor pagination.** monday.com API v2 returns items via `items_page` with a cursor
  and a hard page cap; 344 rows exceeds a single default page.
- **Complexity budgeting.** monday.com meters GraphQL by complexity points, not request
  count. A naive per-question fetch will hit the ceiling.
- **A defined "now".** "This quarter" is meaningless without a reference date and a
  calendar convention. See §9, OQ-4.

## 3. Architecture

### 3.1 Shape

```text
                    +----------------------------+
   browser  ------->|  F09  Streamlit chat UI    |
   (hosted link)    +-------------+--------------+
                                  | user turn
                    +-------------v--------------+
                    |  F06  Agent core           |
                    |  Anthropic tool-use loop   |
                    |  system prompt = the rules |
                    +-------------+--------------+
                       structured tool calls
                    +-------------v--------------+
                    |  F05  Analytics            |<-- F07 cross-board policy
                    |  pandas metrics + query    |
                    |  spec; returns value +     |
                    |  coverage metadata         |--> F08 leadership brief
                    +-------------+--------------+
                    +-------------v--------------+
                    |  F04  Normalization        |
                    |  schema map, cleaners,     |
                    |  DataQualityReport         |
                    +-------------+--------------+
                    +-------------v--------------+
                    |  F02  monday.com client    |
                    |  GraphQL, paging, retry,   |
                    |  TTL cache, read-only gate |
                    +-------------+--------------+
                                  | HTTPS
                          monday.com API v2
                    (Deals board . Work Orders board)
```

`scripts/seed_monday.py` (F03) sits outside this stack. It is the only component that
writes, it runs once at setup time, and it is not importable from the agent package.

### 3.2 The central decision: the LLM chooses, Python computes

Three options were considered for turning a question into a number.

| Option | How | Verdict |
|--------|-----|---------|
| **A. LLM writes pandas/SQL, we execute it** | Model emits code against a DataFrame or DuckDB table | **Rejected.** Arbitrary code execution in a hosted app; non-deterministic output; the messiness rules (junk rows, zero-as-missing, stage/status conflict) would have to be re-derived correctly by the model on every query. Untestable. |
| **B. Fixed set of named metric tools only** | `pipeline_summary()`, `revenue_by_sector()`, … | Safe and testable, but rigid — every unanticipated question becomes "I can't answer that". |
| **C. Named tools + one validated query spec** ✅ | A small set of high-level tools, plus `query_deals(filters, group_by, metric)` where every field, operator and value is validated against the known schema before execution | **Chosen.** Deterministic and fully testable like B, but composable enough to cover unanticipated questions. Invalid specs come back as a correctable error the model can retry. No code execution. |

The consequence worth stating plainly: **the model never performs arithmetic and never
sees a raw row it then has to add up.** It picks a tool and arguments; tested Python
returns `{value, n_used, n_total, excluded, caveats}`; the model's job is to phrase that
as an insight. A hallucinated number is therefore not merely unlikely — there is no path
by which one can be produced.

### 3.3 Data-quality metadata as a first-class return value

Every analytics function returns a `MetricResult`, never a bare float:

```python
MetricResult(
    value=2_305_518_041.0,
    unit="INR",
    n_used=165,              # rows that contributed
    n_total=344,             # rows in scope after filtering
    excluded={"value_missing": 179},
    caveats=["52% of deals in scope have no recorded value; this total is a floor."],
)
```

The system prompt makes surfacing `caveats` non-optional whenever `n_used < n_total`.
Caveats are generated in Python from measured coverage — they are facts, not model
commentary, so they cannot drift or be forgotten.

### 3.4 Fetch-once, compute-locally

Both boards together are ~520 rows, well under a megabyte. Rather than translating each
question into a filtered API query, the client pulls each board in full, normalizes it
once, and caches the resulting DataFrames with a TTL (default 300 s, manual refresh in the
UI).

Justification: fewer API calls, no complexity-limit risk, sub-second analytics, and — most
importantly — normalization and quality measurement happen **once over the whole board**,
so coverage statistics like "165 of 344" are true board-wide rather than an artefact of
whatever the API happened to return. This is not a general-purpose design; it is the right
one at this data size, and §10 records the row count at which it stops being right.

### 3.5 Tech stack

| Layer | Choice | Why this, not the alternative |
|-------|--------|-------------------------------|
| Language | Python 3.12 + uv | Already pinned in the repo; pandas is the right tool for messy tabular cleaning. |
| monday.com access | **Direct GraphQL API v2 via `httpx`** | The brief allows MCP or API. MCP would add a Node runtime and a second process to a hosted deployment, and its tool surface is tuned for board mutation, which we must not do. Direct GraphQL gives explicit control of pagination cursors, retry/backoff, complexity budgeting, and a hard read-only gate — all graded requirements here. See OQ-6 if MCP is specifically wanted. |
| Data handling | pandas | Null semantics, date coercion, groupby. A 38-column board is not worth hand-rolling. |
| LLM | Anthropic Messages API, `claude-sonnet-5` | Native tool use, fast enough for chat, strong instruction-following for the caveat rules. Model ID configurable; `claude-opus-5` selectable for harder reasoning. |
| Validation | pydantic v2 | Tool arguments arrive from a language model — they must be validated at the boundary, not trusted. Also gives `pydantic-settings` for config. |
| UI | Streamlit | Chat UI, streaming and session state in ~150 lines. A FastAPI + React split would triple the surface area for zero graded benefit. Trade-off accepted: less UX control. |
| Hosting | Streamlit Community Cloud | Free, public URL, secrets UI, deploys from GitHub — satisfies "testable without local setup" directly. |
| Testing | pytest + recorded API fixtures | Offline, deterministic, no key needed to run the suite. |

### 3.6 Folder structure

**Layout decision (F01): flat package at the repo root, not `src/`.** Streamlit Community
Cloud installs dependencies and then runs `streamlit run app.py` from the repository root
— it does not `pip install -e .` the project itself. Under a `src/` layout `import
bi_agent` would fail on the deployment target while working locally, which is the worst
possible failure mode. A flat package is importable with no install step anywhere. Cost:
marginally weaker import isolation in tests; acceptable, since the package name is
distinct and nothing shadows it.

```text
bi_agent/
  config.py            # F01  env-based settings, no literals
  errors.py            # F01  typed exception hierarchy
  logging_config.py    # F01  structured logging + secret redaction filter
  monday/
    client.py          # F02  GraphQL transport, auth, retry, read-only gate
    queries.py         # F02  the few GraphQL documents we send
    boards.py          # F02  board/column introspection, paginated fetch, TTL cache
  data/
    schema.py          # F04  board schema as data: title -> field, type, parser
    normalize.py       # F04  cleaners: junk rows, empty->null, #VALUE!, dates, labels
    quality.py         # F04  DataQualityReport, coverage, zero-vs-missing
    repository.py      # F04  fetch -> normalize -> cache; analytics' only input
  analytics/
    spec.py            # F05  validated filter/group_by/metric query spec
    metrics.py         # F05  named metrics, all returning MetricResult
    calendar.py        # F05  fiscal/calendar quarter resolution
    crossboard.py      # F07  side-by-side comparison + join-refusal policy
    briefing.py        # F08  leadership brief assembly
  agent/
    tools.py           # F06  tool schemas exposed to the model
    prompt.py          # F06  system prompt: rules, caveat obligations, clarify policy
    loop.py            # F06  tool-use loop, conversation state, degradation
app.py                 # F09  Streamlit entrypoint (repo root, per layout decision)
scripts/
  seed_monday.py       # F03  one-off writer: xlsx -> monday boards (not agent code)
  record_fixtures.py   # F02  capture live API responses as test fixtures
tests/
  fixtures/            # recorded monday.com JSON + expected golden values
  unit/ integration/
docs/                  # this plan, feature docs, architecture, audits
```

## 4. Data models, tool surface, interfaces

### 4.1 Canonical field names

Board columns are renamed once, in `schema.py`, to stable snake_case fields. The agent and
the tests refer only to these; monday.com column IDs never leak upward.

**Deals** — `deal_name, owner_code, client_code, status, close_date_actual,
closure_probability, deal_value, tentative_close_date, stage, stage_letter, product_type,
sector, created_date`, plus derived `stage_status_consistent` (does the stage agree with
`status == 'Won'`) and `has_value`.

**Work Orders** — `deal_name, customer_code, serial_no, nature_of_work, execution_status,
data_delivery_date, po_date, document_type, start_date, end_date, owner_code, sector,
work_types (list), amount_excl_gst, amount_incl_gst, billed_excl_gst, billed_incl_gst,
collected_incl_gst, to_bill_excl_gst, to_bill_incl_gst, receivable, ar_priority, qty_ops,
qty_po_raw, qty_po_value, qty_po_unit, qty_billed, qty_balance, invoice_status,
billing_month_actual, wo_status_billed, billing_status, last_invoice_date,
last_invoice_no`, plus derived `is_billed`, `billing_pct`, `collection_pct`.

The four all-empty columns are mapped but flagged `always_null=True`, so any question
touching collection timing gets a specific "this field is empty for all 176 records"
answer rather than a silent zero.

### 4.2 Tools exposed to the model

| Tool | Purpose |
|------|---------|
| `describe_data` | Board shapes, available fields, valid values per categorical, coverage per field. Lets the model ground itself instead of guessing field names. |
| `query_deals` | Validated `filters` + `group_by` + `metric` over the deals board. |
| `query_work_orders` | The same over work orders. |
| `pipeline_health` | Composite: open value, stage distribution, stage/status conflicts, ageing. |
| `revenue_and_collections` | Billed vs collected vs receivable, with zero-vs-missing separated. |
| `compare_boards` | Side-by-side on a shared dimension; **refuses row-level joins** and explains why. |
| `leadership_brief` | Assembles the executive summary (F08). |
| `data_quality_report` | What is missing, contradictory or unanswerable, and why. |

### 4.3 Error handling

A typed hierarchy, each mapped to a specific user-facing degradation:

| Exception | Cause | Agent behaviour |
|-----------|-------|-----------------|
| `ConfigError` | Missing/blank token at startup | Fail fast with an actionable message; never start half-configured. |
| `MondayAuthError` | 401 / invalid token | "I cannot authenticate to monday.com" — no retry, no token echoed. |
| `MondayRateLimitError` | 429 / complexity exceeded | Exponential backoff with jitter, then serve stale cache and say it is stale. |
| `MondayUnavailableError` | 5xx, timeout, network | Retry ×3; then answer from cache if present, stating its age; else name the board that failed and answer from the other. |
| `SchemaMismatchError` | Expected column absent | Degrade per-field, not per-board: answer what the remaining fields support, name the missing one. |
| `QuerySpecError` | Model sent an invalid filter or field | Returned to the model as a correctable tool error, not surfaced to the user. |
| `LLMError` | Anthropic API failure | Preserve the conversation, say the reasoning service failed, offer retry. |

The rule throughout: **partial data produces a partial answer plus a statement of what is
missing — never a silent zero and never a crash.**

## 5. External dependencies

| Package | Why |
|---------|-----|
| `httpx` | HTTP/2 GraphQL client with timeouts and connection reuse |
| `pandas` | tabular normalization and aggregation |
| `pydantic` / `pydantic-settings` | tool-argument validation, env config |
| `anthropic` | Messages API tool use |
| `streamlit` | hosted chat UI |
| `openpyxl` | seeding script reads the workbooks *(already added)* |
| `python-dotenv` | local `.env` loading *(already present)* |
| `tenacity` | retry/backoff — provisional; dropped for ~20 lines of hand-rolled backoff if it earns nothing else |
| `pytest`, `pytest-cov`, `respx` | tests; `respx` mocks httpx without a network |

Nothing else without a stated reason.

## 6. Security

- Token read from environment only; `.env` gitignored; `.env.example` documents the keys.
- Read-only gate in the client: the GraphQL document is inspected and any `mutation`
  operation raises before the request is sent. Defence in depth alongside using a
  read-scoped monday.com token.
- Secrets are never logged, never echoed in errors, never placed in the LLM context.
- Board data is business-confidential and **does leave the process** when sent to the
  Anthropic API — an unavoidable consequence of using a hosted LLM, to be stated
  explicitly in the Decision Log rather than glossed over.

## 7. Feature dependency graph and order

```text
F01 config/skeleton
 +-> F02 monday client --> F03 seed boards + live verify
      +-> F04 normalization + quality
           +-> F05 analytics + metrics
                +-> F06 agent core --> F09 UI --> F10 deploy + docs
                +-> F07 cross-board
                +-> F08 leadership brief
```

| # | Feature | Delivers | Why here |
|---|---------|----------|----------|
| **F01** | Config & skeleton | Settings, typed errors, logging, package layout, test harness | Everything imports it; establishes secret handling before any token exists. |
| **F02** | monday.com client | Auth, GraphQL transport, board/column introspection, cursor pagination, retry/backoff, TTL cache, read-only gate, fixture recorder | FR-1..5, FR-16. Fixture-tested offline; live verification unblocks after F03. |
| **F03** | Board seeding | `scripts/seed_monday.py` creates both boards with typed columns from the workbooks, then live-verifies that F02 round-trips them | DL-4. Must precede any live test. Isolated as the only writer. |
| **F04** | Normalization & quality | Schema map, all cleaners, `DataQualityReport`, repository | FR-6..9. The heart of the brief; every measured issue in DATA_PROFILE.md becomes a named cleaner with a regression test. |
| **F05** | Analytics | Query spec, named metrics, fiscal calendar, `MetricResult` coverage | FR-12, FR-14. Golden-value tests computed independently from the workbooks. |
| **F06** | Agent core | Tool schemas, system prompt, tool-use loop, clarifying questions, multi-turn state, degradation | FR-10, FR-11, FR-15, FR-16. Needs real tools to call, so it follows F05. |
| **F07** | Cross-board | Shared-dimension comparison; explicit join refusal with reasons | FR-13. Small and separable; deliberately not folded into F05 so the refusal policy is tested on its own. |
| **F08** | Leadership brief | Deterministic brief assembly + narration + Markdown export | FR-17. Optional requirement, built only once the metrics it composes are proven. |
| **F09** | Streamlit UI | Chat, streaming, caveat rendering, data-quality panel, cache refresh | FR-15, DL-1. Last before deploy — a UI over an unfinished agent is rework. |
| **F10** | Deploy & docs | Streamlit Cloud deployment, README, Decision Log, `FINAL_REQUIREMENT_AUDIT.md`, `FINAL_VALIDATION.md` | DL-1..3. |

Ordering rationale: strictly bottom-up along the data path, so **every feature is testable
the moment it is written**, with no layer beneath it stubbed. The one deviation is F03
sitting between the client and normalization — seeding must happen before anything can be
verified against live monday.com, but it depends on the client's auth plumbing, so it
cannot come first.

## 8. Testing strategy

Per EXECUTION.md: test plan written **before** implementation, tests **executed**, results
recorded as actual output.

- **Unit** — cleaners, parsers, the query-spec validator, calendar resolution.
  Table-driven on real messy values lifted from the workbooks: `#VALUE!`, `''`, the
  `Nezuko` junk row, `BIlled`, `5360 HA`, `NA verbal confirmation for km`,
  `Project Completed`.
- **Integration** — client against `respx`-mocked monday.com responses: pagination across
  cursors, 429 backoff, 5xx retry then stale-cache fallback, auth failure, malformed
  payload, missing column.
- **Golden-value** — metric outputs compared against figures computed independently from
  the workbooks (deal-value sum `2,305,518,041` over exactly 165 rows; 176 unique serials;
  63 zero billed values). These catch silent normalization regressions.
- **Agent** — tool-selection tests with a stubbed LLM; assert that an ambiguous question
  triggers a clarifying question, and that a partial-data answer carries its caveat.
- **Failure paths** — every exception in §4.3 has a test proving the degradation, not just
  the raise.
- **Regression** — every bug found gets a test before its fix.

Coverage target: ≥90% on `data/` and `analytics/` (where correctness is graded), ≥70%
overall. The full suite must run offline with no API key.

## 9. Decisions and open questions

Raised before deciding, per EXECUTION.md. Resolved 2026-08-31.

### 9.1 Resolved

| # | Question | **Decision** |
|---|----------|--------------|
| **OQ-3** | Hosting platform | **Streamlit Community Cloud.** Public GitHub repo accepted; the sample data is masked. |
| **OQ-4** | What "this quarter" means, given that today is 2026-08-31 while the data ends 2026-01 | **Indian fiscal year, April–March** (Q1 Apr–Jun … Q4 Jan–Mar), **anchored to the real clock**. When the resolved period contains no rows, the agent says so explicitly and offers the most recent period that does — it never silently substitutes a different window. Drives `analytics/calendar.py` (F05). |
| **OQ-5** | Which number "revenue" means | **Default to billed value**, name the basis in every answer, and ask the user when the question could genuinely mean pipeline or cash collected. Deal value = pipeline, billed = revenue, collected = cash; the agent must never blur the three. |
| **OQ-6** | MCP vs direct API | **Direct GraphQL API v2** (§3.5). |
| **OQ-7** | Cross-board policy | **Refuse row-level joins**; compare side-by-side on shared dimensions and always state the limitation. A name join would silently inflate revenue (`Sakura` = 27 deal rows). Drives F07. |

### 9.2 Credentials

F01 and F02 are built and tested entirely against recorded fixtures regardless, because
NFR-3 requires the suite to run offline.

| Env var | Needed by | Status |
|---------|-----------|--------|
| `MONDAY_API_KEY` | **F03** (seeding + live verification) | ✅ **present and verified 2026-08-31** |
| `ANTHROPIC_API_KEY` | **F06** (agent core) | ⬜ outstanding — console.anthropic.com → API Keys |

**Live smoke test, 2026-08-31** — `POST https://api.monday.com/v2`, API-Version
`2024-10`, HTTP 200 on all three probes:

- `{ me { id name is_admin } }` → id `83990706`, `is_admin: true`
- `{ boards(limit:100, state:active) }` → **2 boards, both starter defaults**
  (`Your first board`, 4 items; `Subitems of Your first board`, 0 items).
  Neither Deals nor Work Orders exists — **F03 must create both**, confirming DL-4 is real
  work and not a formality.
- `{ complexity { before } }` → ~989,970 points available. The fetch-once design (§3.4)
  costs a negligible fraction of this; complexity is not a binding constraint at this data
  size.

Env var name is `MONDAY_API_KEY` (matching the existing `.env`), not `MONDAY_API_TOKEN`.
`.env` is confirmed gitignored (`.gitignore:155`) and untracked.

**Read-only caveat, now concrete.** The supplied token is a **personal admin token** —
`is_admin: true`, full account permissions. monday.com does not offer a read-only personal
token; credential-level scoping requires a registered app limited to `boards:read` behind
OAuth. FR-5 is therefore enforced *in our client* (§6): any GraphQL document containing a
`mutation` operation raises before the request is sent, and the seeding script — the one
component that legitimately writes — lives outside the agent package on a separate code
path. This is a guarantee about our code, not about the credential. Restated in the
Decision Log. **Open for decision before F03:** whether to add the OAuth `boards:read` app
and close the gap at the credential layer.

**Read-only caveat, recorded deliberately.** monday.com personal access tokens carry the
issuing user's full account permissions; there is no read-only personal token. True
credential-level scoping needs a registered app with only the `boards:read` scope behind
OAuth, which is disproportionate for a prototype. FR-5 is therefore enforced *in our
client* (§6): any GraphQL document containing a `mutation` operation raises before the
request is sent, and the seeding script — the one component that legitimately writes —
lives outside the agent package and uses a separate code path. This is a guarantee about
our code, not about the token. Restated in the Decision Log.

Model default: `claude-sonnet-5`, configurable via `BI_AGENT_MODEL`.

## 10. Definition of Done

**Per feature** — plan doc written first; code implements only that feature; tests written
before implementation and actually executed with output pasted into the feature doc; zero
failing tests; feature doc updated with real results, decisions and limitations;
`STATUS: COMPLETE`; one `feat:` / `test:` / `fix:` commit.

**Project** — every row of `FINAL_REQUIREMENT_AUDIT.md` marked PASS with evidence; full
suite green from a clean `uv sync` on a clean checkout; hosted link reachable and usable
by someone with no local setup; README explains architecture and monday.com board setup;
Decision Log complete within 2 pages including the "leadership updates" interpretation; no
secret in the repository; agent verified to have issued zero mutations.

**Known scale boundary.** The fetch-once design in §3.4 holds while the boards stay in the
low thousands of rows. Past roughly 10k items per board, in-memory caching of the full
board should give way to server-side filtering or a local DuckDB mirror. Recorded here so
the trade-off is deliberate rather than discovered.

## 11. Final validation checklist (F10)

1. `uv sync` on a clean clone; `uv run pytest` fully green, offline, no API key.
2. Live end-to-end against real monday.com boards: five reference questions — pipeline by
   sector this quarter; revenue this year; top deals at risk; billing vs collection; and
   an ambiguous question that must trigger a clarification.
3. Error paths exercised deliberately: bad token, network down mid-session, one board
   deleted, LLM API failure.
4. Read-only proof: the request log for a full session shows no mutation.
5. Caveat proof: every answer touching a sparse field carries its coverage statement.
6. Hosted deployment reachable from a clean browser session.
7. Docs reviewed against the code as built; `FINAL_VALIDATION.md` records environment,
   commands, real output, coverage, and known limitations.
