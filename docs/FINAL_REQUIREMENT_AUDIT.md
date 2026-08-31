# Final Requirement Audit

Every row below cites the requirement (from `docs/00_IMPLEMENTATION_PLAN.md` §2, which
was itself derived from `PROBLEM-STATEMENT.md`), the implementation, and the test that
proves it. Audited 2026-08-31 against the offline suite recorded in
`docs/FINAL_VALIDATION.md` (382 passed).

## Functional requirements

| ID | Requirement | Implementation | Tests | Status |
|----|-------------|-----------------|-------|--------|
| FR-1 | Connect to monday.com via MCP or API | `bi_agent/monday/client.py` (direct GraphQL v2) | `tests/integration/test_client.py` | PASS |
| FR-2 | Handle authentication and connection management | `Settings.monday_api_key`, `MondayAuthError` on 401 | `tests/unit/test_config.py`, `tests/integration/test_client.py::*auth*` | PASS |
| FR-3 | Read all data from both boards | `bi_agent/monday/boards.py` (cursor-paginated `items_page`) | `tests/integration/test_boards.py` | PASS |
| FR-4 | Never hardcode CSV data; query monday.com dynamically | `.xlsx` files used only by `scripts/seed_monday.py`; `bi_agent/data/repository.py` fetches live via `BoardReader` on every cache miss | `tests/integration/test_repository.py` | PASS |
| FR-5 | Read-only — no mutations | `MondayClient` rejects any document containing a `mutation` operation before sending | `tests/unit/test_read_only_gate.py`, `tests/unit/test_write_gate.py` | PASS |
| FR-6 | Handle missing/null values gracefully | `bi_agent/data/normalize.py`, `quality.py`: zero-vs-missing, null coercion | `tests/unit/test_normalize.py`, `tests/unit/test_quality.py` | PASS |
| FR-7 | Normalize inconsistent dates, naming, text | `normalize.py` date coercion + label normalization (e.g. `BIlled` → `Billed`) | `tests/unit/test_normalize.py` | PASS |
| FR-8 | Produce meaningful results from incomplete data | `MetricResult.n_used`/`n_total`/`excluded` on every metric | `tests/unit/test_metrics.py`, `tests/unit/test_spec.py` | PASS |
| FR-9 | Communicate data-quality caveats | `MetricResult.caveats`, `data_quality_report` tool, sidebar panel in `app.py` | `tests/unit/test_metrics.py`, `tests/unit/test_tools.py`, `tests/integration/test_app.py` | PASS |
| FR-10 | Interpret founder-level business questions | `bi_agent/agent/prompt.py` + `loop.py` tool-use loop | `tests/unit/test_loop.py` | PASS |
| FR-11 | Ask clarifying questions when genuinely ambiguous | System prompt policy; loop returns model's clarifying turn without forcing a tool call | `tests/unit/test_loop.py::*clarif*` | PASS |
| FR-12 | Answer on revenue, pipeline health, sector, operational metrics | `bi_agent/analytics/metrics.py` (`pipeline_value`, `revenue_billed`, `collected_amount`, `receivable`, `sector_breakdown`, `stage_distribution`) | `tests/unit/test_metrics.py` | PASS |
| FR-13 | Query across both boards when needed | `bi_agent/analytics/crossboard.py::compare_boards` | `tests/unit/test_crossboard.py` | PASS |
| FR-14 | Context and insight, not just raw numbers | Caveats attached to every `MetricResult`; system prompt requires narrating them | `tests/unit/test_metrics.py`, `bi_agent/agent/prompt.py` | PASS |
| FR-15 | Conversational, multi-turn | `Agent` keeps conversation state across calls; Streamlit `st.session_state` history | `tests/unit/test_loop.py`, `tests/integration/test_app.py` | PASS |
| FR-16 | Graceful API failure handling | Typed hierarchy in `bi_agent/errors.py`: retry/backoff, stale-cache fallback, per-board degradation | `tests/integration/test_client.py` (429/5xx/timeout), `tests/integration/test_repository.py` | PASS |
| FR-17 | *(Optional)* Help prepare leadership updates | `bi_agent/analytics/briefing.py::build_leadership_brief` + `leadership_brief` tool | `tests/unit/test_briefing.py` | PASS |

## Deliverables

| ID | Deliverable | Implementation | Status |
|----|-------------|-----------------|--------|
| DL-1 | Hosted prototype, link-accessible, no local setup | `requirements.txt`, `app.py` at repo root, README §Deploy gives the exact Streamlit Community Cloud steps | **PENDING** — requires the repo owner's own GitHub/Streamlit account; not completable by this codebase alone (see `docs/features/10_DEPLOY_DOCS.md` §7) |
| DL-2 | Decision Log, ≤2 pages | `docs/DECISION_LOG.md` | PASS |
| DL-3 | Source code + README (architecture + monday.com setup) | `README.md`, `docs/architecture/ARCHITECTURE.md` | PASS |
| DL-4 | monday.com boards created from the workbooks with sensible column types | `scripts/seed_monday.py`; live-verified, see `docs/SEEDING_REPORT.md` | PASS |

## Non-functional requirements

| ID | Requirement | Evidence | Status |
|----|-------------|----------|--------|
| NFR-1 | Answer latency < 10s typical | Fetch-once + TTL cache design (§3.4 of the plan); boards are ~500 rows total, in-memory pandas ops are sub-second once cached | PASS (by design; not micro-benchmarked) |
| NFR-2 | Determinism of numbers | Every figure is a tested Python `MetricResult`; the model never computes | PASS — enforced by architecture, see `docs/architecture/ARCHITECTURE.md` |
| NFR-3 | Reproducible offline tests | `pyproject.toml` `addopts = "-m 'not live'"`; full suite runs with no network/API key | PASS — `docs/FINAL_VALIDATION.md` |
| NFR-4 | Secret handling | `.env` gitignored, `.env.example` committed, `SecretStr` fields, redaction filter in `logging_config.py` | PASS — `tests/unit/test_logging_redaction.py` |
| NFR-5 | Read-only safety | Mutation gate in `MondayClient` | PASS — `tests/unit/test_read_only_gate.py`, `tests/unit/test_write_gate.py` |
| NFR-6 | Cost — one fetch per cache window | `BoardRepository` TTL cache | PASS — `tests/integration/test_repository.py` |
| NFR-7 | Observability | Structured logging on every API call, tool call, coercion failure (`bi_agent/logging_config.py`) | PASS |
| NFR-8 | Maintainability — schema as data | `bi_agent/data/schema.py` | PASS |

## Summary

17/17 functional requirements PASS. 3/4 deliverables PASS; DL-1 (hosted link) is the one
item genuinely outside this codebase's reach — it needs a human with a GitHub/Streamlit
account and live API keys to click "Deploy," which the README documents step-by-step.
8/8 non-functional requirements PASS.
