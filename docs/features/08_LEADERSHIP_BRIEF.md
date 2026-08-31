# F08 — Leadership Brief Assembly

STATUS: **COMPLETE** — implemented and verified 2026-08-31. 12 new tests (9 in this
feature's own file, 3 more wired through `agent/tools.py`). Full suite
`uv run pytest -q`: **375 passed** (combined with F07, built in the same pass), 9 live
deselected.

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)
Depends on: [04_NORMALIZATION_ANALYTICS.md](04_NORMALIZATION_ANALYTICS.md),
[06_AGENT_CORE.md](06_AGENT_CORE.md) (both COMPLETE)

---

## 1. Objective

FR-17, the brief's one optional, deliberately open requirement: *"the agent should help
prepare data for leadership updates."* PROBLEM-STATEMENT.md leaves the interpretation to
us and asks it be recorded in the Decision Log. F08's interpretation: a leadership update
is a **deterministic composition of metrics already proven correct in F05** — pipeline,
billed revenue, collected cash, receivable, stage distribution, top sectors, and every
live data-quality caveat — assembled into one structured object plus a ready-to-paste
Markdown summary. Nothing here performs new arithmetic; `build_leadership_brief` calls the
same tested functions `pipeline_health`/`revenue_and_collections` already call, so a
figure in a brief can never drift from the same figure asked about directly.

The alternative considered and rejected: letting the model free-write a brief from several
tool calls in one turn. Rejected for the same reason plan section 3.2 rejects model
arithmetic generally — a model-assembled summary could drop a caveat, round a number, or
silently omit the sectors it didn't like the look of, and none of that would be caught by
anything in this codebase. A deterministically-assembled brief is testable; a
model-narrated one is not.

## 2. Requirement mapping

| Req | Statement | How F08 satisfies it |
|---|---|---|
| **FR-17** | Help prepare data for leadership updates (optional) | `leadership_brief` tool: one call returns the full executive-summary figure set plus rendered Markdown. |
| **FR-9 / FR-14** | Surface caveats; context and insight, not raw numbers | Every constituent `MetricResult`'s caveats ride into both the structured output and the Markdown text (tested explicitly — a caveat present only in the dataclass and dropped from the rendered text would be a regression). |
| **NFR-2** | Determinism | `build_leadership_brief` composes `pipeline_value`, `revenue_billed`, `collected_amount`, `receivable`, `stage_distribution`, `sector_breakdown` verbatim — no arithmetic of its own. |

## 3. Technical design

### 3.1 `bi_agent/analytics/briefing.py`

`build_leadership_brief(deals, work_orders, *, sector=None, period=None, now=None) ->
LeadershipBrief`. `LeadershipBrief` is a frozen dataclass carrying:

- `pipeline`, `revenue_billed`, `collected`, `receivable` — the same `MetricResult`s
  `pipeline_health`/`revenue_and_collections` return.
- `stage_distribution` — from `stage_distribution()`, unchanged.
- `top_sectors_by_pipeline` — top 5 sectors by summed `deal_value`, **always computed
  board-wide**, independent of any `sector` filter: ranking one sector against itself
  once it has been filtered down to just that sector would be meaningless, so the
  filter scopes `pipeline`/`revenue_billed` only.
- `data_quality_caveats` — always-null fields per board, the stage/status conflict count
  phrased as a sentence, and the junk-row exclusion count. Assembled once here rather than
  requiring the model to separately call `data_quality_report` and remember to mention it.
- `markdown` — the same data, rendered as headed sections a founder can paste directly
  into a document. Every caveat on every constituent `MetricResult` is rendered as an
  indented bullet under its figure — the rule that a number without its coverage caveat is
  a wrong answer (CLAUDE.md, prompt.py rule 1) applies to the rendered text too, not just
  the structured fields the model sees.

`period` is passed straight through as the header label (the phrase as asked, e.g. "this
quarter") rather than re-resolved — `pipeline_value`/`revenue_billed` already resolve it
internally and attach a fallback caveat to their own `MetricResult` if the requested period
had no rows (F05 section 3.7), and that caveat rides into the Markdown via the general
caveat-rendering path. Re-resolving separately here would risk a second, inconsistent
source of truth for the same period.

### 3.2 Tool surface (`agent/tools.py`)

`leadership_brief(sector?, period?)`. Returns the dataclass fields as JSON plus
`markdown`. System prompt rule 8 tells the model to call this tool for a leadership-update
request rather than assembling one from several other tool calls, and forbids altering any
figure or caveat the tool returns — it may add prose, not edit numbers.

## 4. Files created / changed

| File | Responsibility |
|---|---|
| `bi_agent/analytics/briefing.py` | `LeadershipBrief`, `build_leadership_brief`. |
| `bi_agent/agent/tools.py` | `leadership_brief` tool schema + dispatcher. |
| `bi_agent/agent/prompt.py` | Rule 8 added: use `leadership_brief` for leadership-update questions, never hand-assemble or edit its figures. |
| `tests/unit/test_briefing.py` | New — golden-value composition, Markdown rendering, sector/period scoping. |
| `tests/unit/test_tools.py` | `leadership_brief` dispatch test. |

## 5. Test plan

| # | Case | Expectation |
|---|---|---|
| 1 | Figures match F05 golden values | Pipeline `2,305,518,040.91`, revenue `126,719,936.37`, collected `90,428,187.50`, receivable `36,291,748.87` — identical to `test_metrics.py`. |
| 2 | Stage distribution | Sums to 344 (all non-junk deals). |
| 3 | Top sectors | Sorted descending, capped at 5. |
| 4 | Stage/status conflicts | `72`, and a matching caveat sentence present. |
| 5 | Always-null fields surfaced | Work Orders' four always-empty columns named in a caveat. |
| 6 | Sector filter scope | Filtering narrows `pipeline`/`revenue_billed`'s `n_total`; top-sector list length is unchanged. |
| 7 | Period fallback | "this quarter" against `now=2026-08-31` (no rows) states the substitution, same as F05. |
| 8 | Markdown completeness | Every section heading present; the `"165 of 344"` coverage caveat appears in the rendered text, not just the dataclass. |
| 9 | All-time label | No `period` given -> `period_label is None`, Markdown header reads "All-time". |
| 10-12 | Tool dispatch | `leadership_brief` tool returns the composed figures and Markdown through `dispatch_tool`. |

## 6. Acceptance criteria

- Every numeric field in `LeadershipBrief` traces to an existing, tested F05 function call
  — no new arithmetic in `briefing.py`.
- No caveat that exists on a constituent `MetricResult` is silently dropped from the
  rendered Markdown.
- Sector filtering never distorts the top-sector ranking into a self-comparison.
- Full suite green.

## 7. Implementation results

Implemented as designed on the first pass — no bugs found beyond the group-key issue
already documented and fixed under F07 (section 3.2 of that doc), which this feature's
`stage_distribution` rendering also benefits from.

**Acceptance criteria, verified:**

| Criterion | Result |
|---|---|
| No new arithmetic | PASS — every field is a call to an existing `analytics.metrics` function; `briefing.py` only sorts, filters `None`, and formats strings |
| No caveat dropped from Markdown | PASS — `test_brief_markdown_contains_every_section_and_no_bare_figures` asserts the `"165 of 344"` text appears in `brief.markdown` |
| Sector filter doesn't distort top-sector ranking | PASS — `test_brief_sector_filter_scopes_pipeline_and_revenue_only` |
| Full suite green | PASS — 375 passed, 0 failed |

## 8. Known limitations

- **"Leadership update" is one fixed shape** (pipeline, revenue/collections, stage
  distribution, top 5 sectors, data-quality notes) — a founder wanting a different cut
  (e.g. an at-risk-deals-only brief) still needs to ask a direct question; this tool is
  not a general report builder.
- **The Markdown is not narrated** — it is headings and bullet figures, deliberately
  unstyled prose, so the model's own words (added around it, per prompt rule 8) are what
  make it read as a written update rather than a data dump. Untested here because that
  narration happens in the live model, not this layer (same boundary F06 already documents
  for its own untested live behaviour).
- **No export beyond the returned string** — no PDF/DOCX rendering, no persistence of a
  generated brief. Out of scope for a prototype; the Markdown is copy-paste ready, which
  satisfies FR-17 as interpreted without adding a file-generation surface.
