# F04+F05 — Normalization & Quality, and Analytics

STATUS: **COMPLETE** — implemented and verified 2026-08-31. 80 new tests, all against the
real seeded boards (re-recorded fixtures, `tests/fixtures/live/{deals,work_orders}_board_*.json`).
Full suite `uv run pytest -q`: **336 passed**, 9 live deselected — zero failures, zero
regressions in F01-F03.

Combined into one doc because they share a
single artifact neither can be reviewed without: the canonical schema (§3.2). F04 defines
it, F05 is the first and only consumer of it, and every metric F05 returns is a direct
function of a cleaning rule F04 applies. Splitting them into two docs would mean copying
the schema table twice and reviewing half a decision each time.

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)
Depends on: [03_BOARD_SEEDING.md](03_BOARD_SEEDING.md) (COMPLETE — live boards seeded and
verified 2026-08-31: Deals 346 items, Work Orders 176 items)

---

## 1. Objective

F04 turns monday.com's raw `column_values` payload (opaque IDs, `{"text": ..., "value":
...}` pairs, the seeded junk rows, the zero-vs-missing distinction) into two clean pandas
DataFrames with stable snake_case field names, plus a `DataQualityReport` naming every gap.
F05 is the only thing allowed to compute a number from those frames: a small set of named
metrics plus one validated query spec, every one returning a `MetricResult` that carries its
own coverage and caveats rather than a bare float.

Together they are what makes plan §3.2's central decision ("the LLM chooses, Python
computes") real: F06 will pick a tool and arguments, but every number and every caveat
sentence is produced here, deterministically, before the model ever sees it.

Two things this pair is *not*:

- **Not a second copy of F03's transport logic.** F04 reads through F02's `BoardReader`
  exactly as F03's own verification step does; it adds no new monday.com calls.
- **Not free-form aggregation.** F05 exposes named metrics and one constrained query spec
  (plan §3.2 option C) — never a path where the model's own arithmetic or a
  model-generated filter expression reaches a number a founder sees.

## 2. Requirement mapping

| Req | Statement | How this pair satisfies it |
|-----|-----------|------------------------------|
| **FR-6** | Handle missing/null values gracefully | Every parser distinguishes empty (`None`) from unparseable (`#VALUE!`, junk text) from a real zero; `DataQualityReport` counts all three per field. |
| **FR-7** | Normalize inconsistent dates, naming, text | `normalize.py`: date parsing, the `BIlled`→`Billing Status` casing fix (raw value kept alongside), stage-letter extraction, junk-row exclusion. |
| **FR-8** | Meaningful results from incomplete data | `MetricResult.n_used`/`n_total`/`excluded` on every metric; a field that is 0/176 populated never silently returns 0. |
| **FR-9** | Communicate data-quality issues | `DataQualityReport` (F04) plus per-metric `caveats` (F05), generated from measured coverage, not phrased by the model. |
| **FR-12** | Revenue, pipeline health, sector, operational metrics | `metrics.py`: `pipeline_value`, `revenue_billed`, `collected_amount`, `receivable`, `stage_distribution`, `sector_breakdown`. |
| **FR-14** | Context and insight, not raw numbers | `MetricResult` is structured precisely so F06 has trend/coverage material to narrate with, not a lone number. |
| **NFR-1** | Latency | `repository.py` fetches once per TTL window and normalizes once; every metric runs against an in-memory frame. |
| **NFR-2** | Determinism | Every figure is tested Python (pandas aggregation); the model never computes. |
| **NFR-3** | Offline tests | Normalization and metrics are tested against recorded board fixtures (re-recorded from the real seeded boards, F03 §3.8's promise), no network. |
| **NFR-8** | Schema as data | `schema.py`: one table per board, `monday header -> canonical field -> parser -> flags`. |

## 3. Technical design

### 3.1 Module layout

```text
bi_agent/data/
  schema.py       canonical field table per board (NFR-8)
  normalize.py    column_values -> typed value; junk-row/casing/zero-vs-missing rules
  quality.py      DataQualityReport: coverage, always-null fields, conflicts, unrepresentable
  repository.py   BoardReader -> normalize -> cached DataFrame; analytics' only input
bi_agent/analytics/
  spec.py         MetricResult, QuerySpec (validated filter/group_by/metric)
  calendar.py     Indian fiscal-year period resolution ("this quarter" -> date range)
  metrics.py      named metrics + the generic query engine, all returning MetricResult
tests/fixtures/live/
  deals_board_items.json, work_orders_board_items.json   re-recorded from the real boards
```

### 3.2 Canonical schema (the shared artifact)

Source headers are exactly the monday.com column titles F03 created (`scripts/seeding/schema.py`).
`always_null` columns are the four F03 §3.4 created anyway so they can be reported empty rather
than absent (CLAUDE.md).

**Deals** (item name = `Deal Name`):

| Canonical field | Source header | Type | Notes |
|---|---|---|---|
| `owner_code` | Owner code | text | |
| `client_code` | Client Code | text | |
| `status` | Deal Status | text | Never trusted alone — cross-checked against `stage` (§3.4). |
| `close_date_actual` | Close Date (A) | date | 92% null. |
| `closure_probability` | Closure Probability | text | High/Medium/Low + junk. |
| `deal_value` | Masked Deal value | number | 52% null; zero never observed in source but treated the same as any number (§3.4). |
| `tentative_close_date` | Tentative Close Date | date | |
| `stage` | Deal Stage | text | 17 values incl. junk; `stage_letter` derived. |
| `product_type` | Product deal | text | 49% null. |
| `sector` | Sector/service | text | 12 values incl. junk. |
| `created_date` | Created Date | date | |
| `source_row` | Source Row | text | Provenance only (F03); never business data. |

Derived: `stage_letter` (`"A"` from `"A. Lead Generated"`, `None` for the unprefixed
`Project Completed` and for junk), `has_value` (`deal_value is not None`),
`stage_status_consistent` (bool, §3.4), `is_junk` (bool, §3.3).

**Work Orders** (item name = `Serial #`):

| Canonical field | Source header | Type | Notes |
|---|---|---|---|
| `deal_name` | Deal name masked | text | Not unique; not a join key (CLAUDE.md). |
| `customer_code` | Customer Name Code | text | |
| `nature_of_work` | Nature of Work | text | |
| `last_recurring_month` | Last executed month of recurring project | text | 15/176 populated. |
| `execution_status` | Execution Status | text | 7 values. |
| `data_delivery_date` | Data Delivery Date | date | 58/176. |
| `po_date` | Date of PO/LOI | date | |
| `document_type` | Document Type | text | |
| `start_date` | Probable Start Date | date | |
| `end_date` | Probable End Date | date | tail runs to 2028-03-31 — outlier, never silently dropped. |
| `owner_code` | BD/KAM Personnel code | text | |
| `sector` | Sector | text | 6 values (fewer than Deals' 12 — cross-board sector lists differ). |
| `work_types` | Type of Work | list[text] | Comma-split; 36 raw strings over ~12 atoms. |
| `skylark_platform` | Is any Skylark... deliverables...? | text | |
| `last_invoice_date` | Last invoice date | date | |
| `last_invoice_no` | latest invoice no. | text | |
| `amount_excl_gst` | Amount in Rupees (Excl of GST) (Masked) | number | 1 literal `#VALUE!`. |
| `amount_incl_gst` | Amount in Rupees (Incl of GST) (Masked) | number | |
| `billed_excl_gst` | Billed Value in Rupees (Excl of GST.) (Masked) | number | |
| `billed_incl_gst` | Billed Value in Rupees (Incl of GST.) (Masked) | number | 63 real zeros. |
| `collected_incl_gst` | Collected Amount in Rupees (Incl of GST.) (Masked) | number | |
| `to_bill_excl_gst` | Amount to be billed in Rs. (Exl. of GST) (Masked) | number | |
| `to_bill_incl_gst` | Amount to be billed in Rs. (Incl. of GST) (Masked) | number | |
| `receivable` | Amount Receivable (Masked) | number | 77 zeros. |
| `ar_priority` | AR Priority account | text | 10/176. |
| `qty_ops_raw` | Quantity by Ops | text | Not summable. |
| `qty_po_raw` | Quantities as per PO | text | Mixed units — never summed (CLAUDE.md). |
| `qty_billed_raw` | Quantity billed (till date) | text | Not summable. |
| `qty_balance_raw` | Balance in quantity | text | Not summable. |
| `invoice_status` | Invoice Status | text | Incl. one-offs (`Billed- Visit 7`). |
| `expected_billing_month` | Expected Billing Month | text | **always_null.** |
| `billing_month_actual` | Actual Billing Month | text | 70/176. |
| `actual_collection_month` | Actual Collection Month | text | **always_null.** |
| `wo_status_billed` | WO Status (billed) | text | 102/176. |
| `collection_status` | Collection status | text | **always_null.** |
| `collection_date` | Collection Date | date | **always_null.** |
| `billing_status` | Billing Status | text | Casing bug `BIlled` normalized to `Billed`; `billing_status_raw` keeps the original. |
| `source_row` | Source Row | text | Provenance. |

Derived: `is_billed` (`billed_incl_gst is not None and billed_incl_gst > 0`),
`billing_pct` (`billed_incl_gst / amount_incl_gst` when the denominator is a positive
number, else `None`), `collection_pct` (`collected_incl_gst / billed_incl_gst`, same rule).

### 3.3 Reading `column_values`: `text`, not `value`

monday.com's `column_values` carries both `text` (display string) and `value` (raw JSON).
`normalize.py` parses from **`text`** uniformly across all three column types — the same
field F03's own verification step sums numbers from (`scripts/seed_monday.py::_sum_numbers_column`)
— rather than branching per-type on `value`'s JSON shape. One parser path per type, one
thing to get right, and it is already proven correct by F03 §3.8's round-trip checks.

| Column type | Empty `text` | Unparseable `text` (e.g. `#VALUE!`, junk) | Real value |
|---|---|---|---|
| `date` | `None` | `None` + recorded in quality report | `date.fromisoformat(text)` |
| `numbers` | `None` | `None` + recorded (never coerced to 0) | `float(text)` (0 stays 0 — FR-6/CLAUDE.md) |
| `text` | `None` | n/a (every string is valid text) | `text` verbatim (or `text.split(",")` for `work_types`) |

### 3.4 Cleaning rules (every one traces to CLAUDE.md / DATA_PROFILE.md)

| Rule | Rationale |
|---|---|
| **Junk rows excluded from every metric, counted in the quality report.** Detected on `status.text == "Deal Status"` — the same marker F03 seeded them under. | CLAUDE.md: junk rows leak `"Sector/service"` as a sector, `"Closure Probability"` as a probability. F03 deliberately transported them (D-1); F04 is where they get filtered *for aggregation* while staying visible for the data-quality story. |
| **`status` never trusted alone.** `stage_status_consistent = status != "Won" or stage in WON_CONSISTENT_STAGES`, where `WON_CONSISTENT_STAGES = {"G. Project Won", "H. Work Order Received", "Project Completed", "J. Invoice sent", "K. Amount Accrued"}` (the self-consistent stages measured in DATA_PROFILE.md). | 70 of 165 `Won` deals sit at `A. Lead Generated` — CLAUDE.md: "cross-check the stage and tell the user when the two disagree." |
| **Zero is a value, not a gap.** A `numbers` column with `text == "0"` parses to `0.0`, counted in `n_used`. | The 63-work-order case CLAUDE.md calls out by name. |
| **`#VALUE!` (and any other unparseable numeric text) parses to `None`, recorded as `unrepresentable`, never coerced to 0.** | Same file, same principle, applied at read time instead of seed time. |
| **The four always-null columns are flagged, not silently aggregated.** Any metric that would touch `collection_status`/`collection_date`/`expected_billing_month`/`actual_collection_month` returns a `MetricResult` whose `caveats` say the field is empty for all N records. | CLAUDE.md: "Any collection-timing question is unanswerable — say so." |
| **`qty_*_raw` fields are never summed.** `QuerySpec` validation rejects `metric="sum"` on any `*_raw` quantity field with a `QuerySpecError` naming why (mixed units, three spellings of "acre"). | CLAUDE.md: "Not summable; parse unit-by-unit or refuse." Refuse, for this iteration — unit-aware parsing is out of scope, recorded in §10. |
| **`BIlled` normalized to `Billed` for grouping; `billing_status_raw` keeps the original.** | CLAUDE.md: "normalize... labels before aggregating" — but the casing bug itself is exactly the kind of finding `data_quality_report` (F06 tool) must be able to surface, so the raw value is not discarded. |
| **`Probable End Date` outliers (2028-03-31) are not dropped or clipped.** Any period-scoped query is bounded by the resolved date range (§3.6), which naturally excludes them from a "this quarter" answer without deleting the record. | CLAUDE.md: "check outliers before framing anything as 'this quarter'." |
| **Sector vocabularies are not reconciled across boards.** Deals carries 12 sectors, Work Orders 6; `sector_breakdown` runs per board and is never merged into one shared category set. | DATA_PROFILE.md; matches plan OQ-7's side-by-side-only cross-board policy (F07, not this feature). |

### 3.5 `DataQualityReport`

```python
@dataclass(frozen=True)
class FieldCoverage:
    field: str
    n_total: int
    n_present: int
    n_unrepresentable: int   # parsed but rejected (#VALUE!, date-typed junk text, ...)
    always_null: bool

@dataclass(frozen=True)
class DataQualityReport:
    board: str
    n_total_rows: int
    n_junk_rows_excluded: int
    coverage: dict[str, FieldCoverage]
    stage_status_conflicts: int      # Deals only
    casing_fixes: dict[str, int]     # e.g. {"BIlled -> Billed": <count>}
    generated_at: datetime
```

Built once per `repository.py` fetch, cached alongside the frame, and exposed as its own
agent tool in F06 (`data_quality_report`) — not recomputed per question.

### 3.6 `analytics/spec.py` — `MetricResult` and `QuerySpec`

```python
@dataclass(frozen=True)
class MetricResult:
    value: float | int | None
    unit: str                       # "INR" | "count" | "ratio" | ...
    n_used: int
    n_total: int
    excluded: dict[str, int]        # reason -> count, e.g. {"value_missing": 179, "junk_row": 2}
    caveats: list[str]
    basis: str | None = None        # for money: "billed" | "deal_value" | "collected" (OQ-5)

class Filter(BaseModel):
    field: str
    op: Literal["eq", "ne", "in", "gt", "gte", "lt", "lte", "is_null", "not_null"]
    value: Any = None

class QuerySpec(BaseModel):
    board: Literal["deals", "work_orders"]
    filters: list[Filter] = []
    group_by: list[str] = []
    metric: Literal["count", "sum", "avg", "min", "max"]
    field: str | None = None        # required unless metric == "count"
```

Validation (raising `QuerySpecError` with a `hint` addressed to the model, per F01):
`field`/`group_by` entries must name a canonical column that exists on `board`'s schema;
a non-`count` metric's `field` must be numeric *and not* one of the `qty_*_raw` fields
(§3.4); an `eq`/`in` filter's `value` against a categorical field is checked against the
set of values actually observed (a typo'd sector name fails fast, with the valid list in
the hint, rather than silently matching zero rows).

### 3.7 `analytics/calendar.py` — Indian fiscal year, anchored to the real clock

Per plan §9.1 OQ-4: FY = April–March, Q1 Apr–Jun … Q4 Jan–Mar. `resolve_period(text, now)`
understands `"this quarter"`, `"last quarter"`, `"this year"`/`"this fiscal year"`,
`"last year"`, and an explicit `"FY25-26"` / `"FY25-26 Q3"`. If the resolved range has zero
rows in the frame being queried, the caller (a `metrics.py` function, not `calendar.py`
itself — it has no access to the data) falls back to the most recent period that does have
rows and says so explicitly in `caveats`; it never silently substitutes a different window.

### 3.8 `analytics/metrics.py` — named metrics + the query engine

| Function | Purpose |
|---|---|
| `run_query(spec, repository)` | The general path (plan §3.2 option C): filter → group_by → aggregate, always excluding junk rows first, always returning `MetricResult`(s). |
| `pipeline_value(repository, *, sector=None, status=None, period=None)` | Sum of `deal_value` over open/filtered deals; `basis="deal_value"`. |
| `revenue_billed(repository, *, sector=None, period=None)` | Sum of `billed_incl_gst`; `basis="billed"` (OQ-5 default for "revenue"). |
| `collected_amount(repository, *, period=None)` | Sum of `collected_incl_gst`; `basis="collected"`. |
| `receivable(repository)` | Sum of `receivable`. |
| `stage_distribution(repository)` | Count per `stage`, plus the `stage_status_conflicts` figure surfaced as a caveat. |
| `sector_breakdown(repository, board, metric)` | Per-sector `MetricResult`s, one board at a time (never merged — §3.4). |
| `always_null_fields(repository, board)` | The four-column list, so F06 can answer "is this tracked at all" directly. |

Every function is a thin, tested wrapper around `run_query` plus the period/fallback logic
in §3.7 — no metric hand-rolls its own filtering.

### 3.9 `data/repository.py` — fetch once, normalize once, cache

Mirrors F02's `BoardReader` cache shape (plan §3.4 fetch-once-compute-locally): one
`BoardRepository` per process, `deals()`/`work_orders()` return `(DataFrame,
DataQualityReport)` from cache within the TTL window, or fetch via `BoardReader` + normalize
on expiry/miss. Analytics never calls `BoardReader` directly.

## 4. Files to create

| File | Responsibility |
|---|---|
| `bi_agent/data/schema.py` | §3.2 table: header → canonical field → type → flags. |
| `bi_agent/data/normalize.py` | `text` → typed value per §3.3; junk/casing/derived-field rules §3.4. |
| `bi_agent/data/quality.py` | `DataQualityReport`, `FieldCoverage`. |
| `bi_agent/data/repository.py` | `BoardRepository`: fetch → normalize → cache. |
| `bi_agent/analytics/spec.py` | `MetricResult`, `Filter`, `QuerySpec` + validation. |
| `bi_agent/analytics/calendar.py` | Fiscal period resolution. |
| `bi_agent/analytics/metrics.py` | `run_query` + named metrics (§3.8). |
| `tests/unit/test_normalize.py` | Parser rules, junk detection, casing fix, zero-vs-missing, `#VALUE!`. |
| `tests/unit/test_quality.py` | Coverage counts, always-null flags, conflict counting. |
| `tests/unit/test_spec.py` | `QuerySpec` validation: unknown field, non-numeric sum target, bad categorical value, `qty_*_raw` refusal. |
| `tests/unit/test_calendar.py` | Fiscal quarter/year resolution against fixed "now" values. |
| `tests/unit/test_metrics.py` | Golden-value tests: deal-value sum, stage distribution, sector breakdown, always-null caveats. |
| `tests/integration/test_repository.py` | Fetch → normalize → cache against a fixture board snapshot. |

Fixtures: re-record `tests/fixtures/live/list_boards.json`, `board_columns.json`,
`board_items_page1.json` (already the pattern F02's `record_fixtures.py` uses) against the
now-real Deals and Work Orders boards, per F03 §3.8's outstanding promise — this is where
that happens.

## 5. Implementation plan

1. Re-run `scripts/record_fixtures.py --board Deals` and `--board "Work Orders"` against the
   live seeded boards; save as the fixtures these tests are written against.
2. `schema.py` — the table, no logic.
3. `normalize.py` + `test_normalize.py`, table-driven on real values pulled from the
   fixtures (`#VALUE!`, `BIlled`, a junk row, a real zero).
4. `quality.py` + `test_quality.py`.
5. `repository.py` + `test_repository.py`.
6. `spec.py` + `test_spec.py` — validation before any metric exists to call it.
7. `calendar.py` + `test_calendar.py`.
8. `metrics.py` + `test_metrics.py`, golden values cross-checked against the F03 verification
   numbers already proven live (346/176 items, the deal-value sum).
9. Run the full suite; fix; document actual results here; mark `STATUS: COMPLETE`.

## 6. Decisions needing confirmation

| # | Decision | Recommendation |
|---|----------|-----------------|
| **D-6** | Which stages count as "self-consistent" with `Won`, for `stage_status_consistent`? | The five stages DATA_PROFILE.md measured as self-consistent (§3.4). Any deal `Won` at another stage (overwhelmingly `A. Lead Generated`) is flagged, not hidden. |
| **D-7** | Unit-aware parsing of `Quantities as per PO`? | **No, refuse.** Three spellings of "acre" plus prose values (`NA verbal confirmation for km`) make unit normalization a project of its own; `QuerySpec` rejects a sum on it with a message naming why, which is honest and cheap. Revisit only if a specific question demands it. |
| **D-8** | Parse from `text` or `value` in `column_values`? | **`text`**, uniformly (§3.3) — already the field F03's own live verification trusted for numeric round-tripping. |

## 7. Acceptance criteria

- Both boards normalize to DataFrames with every canonical field populated or explicitly
  `None`; zero counts distinguish "0 recorded" from "not recorded" everywhere CLAUDE.md
  names (Billed Value, Amount Receivable, etc.).
- The two junk deal rows are excluded from every metric and counted in
  `DataQualityReport.n_junk_rows_excluded == 2`.
- `stage_status_conflicts` reproduces DATA_PROFILE.md's measured 70+2 = 72 non-self-consistent
  `Won` deals.
- `pipeline_value()` over all deals with a value reproduces the F03-verified sum
  (2,305,518,040.91) over 165 deals.
- A query against any `qty_*_raw` field with `metric != "count"` raises `QuerySpecError`.
- A query touching `collection_status`/`collection_date` returns a `MetricResult` whose
  `caveats` state the field is empty for all records — never a bare `0`.
- Full suite green, offline, no network.

## 8. Implementation results

Implemented as designed: `bi_agent/data/{schema,normalize,quality,repository}.py`,
`bi_agent/analytics/{spec,calendar,metrics}.py`. Added `pandas` via `uv add pandas`
(already listed as a planned dependency in the master plan).

Fixtures re-recorded from the live boards per step 1 (`scripts/record_fixtures.py --board
Deals` / `--board "Work Orders"`), saved as `tests/fixtures/live/{deals,work_orders}_board_{columns,items}.json`.

**Acceptance criteria, verified against the real boards:**

| Criterion | Result |
|---|---|
| Both boards normalize fully; zero vs. not-recorded distinguished | PASS — `test_billed_value_zero_is_counted_not_missing` (63 zeros, none treated as missing) |
| 2 junk rows excluded from every metric, counted in the report | PASS — `DataQualityReport.n_junk_rows_excluded == 2`; `stage_status_conflicts` and every `run_query` result confirmed to exclude them |
| `stage_status_conflicts` reproduces DATA_PROFILE.md's 70+2=72 | PASS — `test_deals_report_stage_status_conflicts_matches_data_profile` |
| `pipeline_value()` reproduces the F03-verified sum | PASS — `2,305,518,040.91` over 165 deals, exact match |
| `qty_*_raw` sum raises `QuerySpecError` | PASS — with the mixed-units reason named, not a generic "not numeric" (a real bug caught by `test_sum_on_a_non_summable_quantity_field_is_rejected` and fixed — see below) |
| Always-null fields never return a bare 0 | PASS — `collected_amount()` and any query touching the four always-null columns carry an explicit caveat |
| Full suite green, offline | PASS — 336 passed, 0 failed |

**One real bug found by the test suite before it ever touched a live board:** the
`qty_*_raw` fields are schema-typed `"text"` (correctly — they are free text, not
malformed numbers), so `QuerySpec` validation's numeric-type check fired first and
reported "not numeric" instead of naming the actual reason (mixed/inconsistent units).
Fixed by checking `FieldSpec.summable` before the type check, so a field explicitly
flagged non-summable always gets the specific, actionable message. Recorded here per
EXECUTION.md's "every bug found gets a test before its fix" — the regression test is
`test_sum_on_a_non_summable_quantity_field_is_rejected`.

**Golden work-order figures observed** (for reference, not asserted as fixed constants
elsewhere): billed (incl. GST) = 126,719,936.37 over 176 rows; collected (incl. GST) =
90,428,187.50 over 78 rows; receivable = 36,291,748.87 over 176 rows; 3 `BIlled` casing
fixes. All match F03's independently-verified round-trip sums exactly.

## 9. Known limitations

- **Period resolution (`resolve_period`) understands a fixed vocabulary** ("this/last
  quarter", "this/last fiscal year", explicit `FY25-26[ Q3]`) — no natural-language date
  parsing beyond that. F06's system prompt will need to normalize looser phrasing
  ("Q2 this year", "the last three months") onto this vocabulary or ask a clarifying
  question, per FR-11.
- **`pipeline_value`'s period filter defaults to `tentative_close_date`**; this is a
  judgment call (deals *expected* to land in a period), not stated explicitly in the
  brief. Callers can override `date_field`.
- **Cross-board sector reconciliation is out of scope here, correctly** — `sector_breakdown`
  runs per board and the two vocabularies (12 vs. 6 sectors) are never merged, per plan
  OQ-7. F07 owns any cross-board comparison.
- **`run_query`'s `group_by` does not currently apply the junk-row/period caveats per
  group** — only the ungrouped path attaches the "N junk rows excluded" note. A grouped
  `stage_distribution()` is still correct (junk rows are filtered out before grouping),
  just without a per-group caveat string; acceptable since junk-row exclusion is a
  board-level fact, not a per-group one.
- **No unit-aware parsing of `qty_po_raw`** (decision D-7, deliberate) — any question
  requiring a real quantity total across mixed units is out of scope for this iteration.
