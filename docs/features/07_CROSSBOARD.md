# F07 — Cross-Board Comparison and Join Refusal

STATUS: **COMPLETE** — implemented and verified 2026-08-31. 13 new tests (7 in this
feature's own file, 6 more wired through `agent/tools.py` and its existing suite). Full
suite `uv run pytest -q`: **375 passed**, 9 live deselected.

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)
Depends on: [04_NORMALIZATION_ANALYTICS.md](04_NORMALIZATION_ANALYTICS.md),
[06_AGENT_CORE.md](06_AGENT_CORE.md) (both COMPLETE)

---

## 1. Objective

FR-13 ("Query across both boards when needed") plus OQ-7's resolved policy: **refuse
row-level joins, compare side-by-side on shared dimensions, always state the
limitation**. CLAUDE.md is explicit about why a join is unsafe — `Serial #` is the only
true primary key anywhere in the data and Deals have no equivalent; 346 deal rows carry
only 155 distinct names (`Sakura` alone is 27 rows), so joining on name would silently
multiply revenue. F07 makes that refusal structural rather than a prompt-only promise: the
comparison function only accepts dimensions genuinely shared between the boards, and
anything else — most importantly a disguised join request like `dimension="deal_name"` —
fails as a correctable tool error naming exactly why.

## 2. Requirement mapping

| Req | Statement | How F07 satisfies it |
|---|---|---|
| **FR-13** | Query across both boards when needed | `compare_boards` tool: independent per-board aggregation on a shared dimension, returned side by side. |
| **OQ-7** | Cross-board policy (resolved) | `CROSSBOARD_DIMENSIONS = {"sector", "owner_code"}` is the only accepted axis; every other dimension raises `QuerySpecError` with a hint naming the duplicate-name problem. |
| **FR-9** | Communicate data-quality issues and caveats | Every `CrossBoardComparison` carries a "not a row-level join" caveat plus a named list of dimension values seen on only one board (e.g. `Aviation` is deals-only; `OWNER_008` is work-orders-only). |

## 3. Technical design

### 3.1 `bi_agent/analytics/crossboard.py`

`compare_boards(deals, work_orders, *, dimension, deals_metric="sum",
deals_field="deal_value", wo_metric="sum", wo_field="billed_incl_gst") ->
CrossBoardComparison`. Internally it is two independent calls to F05's `run_query` — one
per board, `group_by=[dimension]` — never anything that touches both frames at once.
There is no code path that could accidentally join a row from one board to a row from the
other; the function structurally cannot produce that number.

`CrossBoardComparison` carries `deals`/`work_orders` (dimension value -> `MetricResult`),
`deals_only_keys`/`work_orders_only_keys` (the asymmetry CLAUDE.md documents — 12 sectors
on Deals vs. 6 on Work Orders, `OWNER_008` present only on Work Orders), and `caveats`.

### 3.2 A latent bug fixed along the way

`run_query`'s single-column `group_by` was returning pandas 1-tuple keys —
`{"('Won',)": ...}` instead of `{"Won": ...}` — inherited from `frame.groupby([col])`
always yielding tuple keys regardless of list length. This shipped silently in F05/F06
because every existing test summed grouped *values*, never inspected the *keys*.
Comparing two boards' group keys as sets (this feature's core operation) surfaced it
immediately: `'Aviation' in comparison.deals_only_keys` failed because the actual key was
`"('Aviation',)"`. Fixed at the source in `metrics.run_query` (`bi_agent/analytics/metrics.py`,
the group_by branch) rather than papered over in `crossboard.py`, so `query_deals`/
`query_work_orders`'s existing grouped output and `pipeline_health`'s stage distribution
are also clean now — no golden value changed, only the label attached to it.

### 3.3 Tool surface (`agent/tools.py`)

`compare_boards(dimension, deals_metric?, deals_field?, wo_metric?, wo_field?)`. The JSON
schema's `dimension` enum (`["sector", "owner_code"]`) is the first line of defense for a
well-behaved model; `crossboard.py`'s own validation is the real one, since `dispatch_tool`
calls straight into the Python function regardless of what the model sent.

### 3.4 System prompt (`agent/prompt.py`)

Rule 5 rewritten: cross-board questions now route to `compare_boards` instead of being
refused outright; the refusal is scoped to *row-level* combination specifically.

## 4. Files created / changed

| File | Responsibility |
|---|---|
| `bi_agent/analytics/crossboard.py` | `compare_boards`, `CrossBoardComparison`, `CROSSBOARD_DIMENSIONS`. |
| `bi_agent/analytics/metrics.py` | Group-key unwrap fix in `run_query` (section 3.2). |
| `bi_agent/agent/tools.py` | `compare_boards` tool schema + dispatcher. |
| `bi_agent/agent/prompt.py` | Rule 5 updated to point at the new tool. |
| `tests/unit/test_crossboard.py` | New — `compare_boards` behaviour, join refusal. |
| `tests/unit/test_tools.py` | `compare_boards` dispatch tests; removed the now-false "not exposed yet" test. |

## 5. Test plan

| # | Case | Expectation |
|---|---|---|
| 1 | Compare on `sector` | Both sides populated; deals sum matches the F05-verified pipeline total. |
| 2 | Asymmetric sectors | `Aviation` (deals-only, per CLAUDE.md) appears in `deals_only_keys`. |
| 3 | Join-refusal caveat | "not a row-level join" present on every comparison. |
| 4 | `dimension="deal_name"` | `QuerySpecError`, hint names the `Sakura` duplicate-count problem and `no reliable row-level key`. |
| 5 | `dimension="serial_no"` | `QuerySpecError`, hint lists the valid dimensions. |
| 6 | `CROSSBOARD_DIMENSIONS` sanity | Both entries are canonical fields present on *both* boards' schemas. |
| 7 | Compare on `owner_code`, count | `OWNER_008` (work-orders-only, per CLAUDE.md) appears in `work_orders_only_keys`. |
| 8-9 | Tool dispatch | `compare_boards` tool returns the same shape; `dimension="deal_name"` returns a correctable `{"error", "hint"}`, not a crash. |

## 6. Acceptance criteria

- No code path can produce a row-joined figure — `compare_boards` never merges the two
  frames.
- A disguised join request fails loudly with a reason, not silently with a wrong number.
- Grouped tool output uses clean scalar keys everywhere (regression-covered by the fix in
  section 3.2, not just this feature's own tests).
- Full suite green.

## 7. Implementation results

Implemented as designed. The group-key bug (section 3.2) was the only surprise; found by
the first real cross-board key-set comparison this codebase has ever run, fixed at the
source, and confirmed to not move any existing golden value — `uv run pytest -q` before
and after the fix showed identical pass counts outside the two new failing assertions that
the fix itself resolved.

**Acceptance criteria, verified:**

| Criterion | Result |
|---|---|
| No row-joined figure is reachable | PASS — `compare_boards` calls `run_query` once per board; nothing merges the two `DataFrame`s |
| Disguised join fails loudly | PASS — `test_compare_boards_rejects_deal_name_join`, `test_compare_boards_rejects_unknown_dimension` |
| Clean grouped keys | PASS — `test_compare_boards_owner_code_counts` asserts `["OWNER_008"]`, not tuple-string form |
| Full suite green | PASS — 375 passed, 0 failed; live suite (9) still deselected/green |

## 8. Known limitations

- **Only two comparison dimensions exist** (`sector`, `owner_code`) because those are the
  only fields the two boards carry under the same canonical name. A question like "compare
  by client" has no safe axis (`Client Code` vs. `WOCOMPANY_002`-style codes are unrelated
  across boards, CLAUDE.md) and is not offered.
- **`deals_only_keys`/`work_orders_only_keys` include a stringified `NaN` group** when a
  board has rows with no recorded dimension value (`dropna=False` in `run_query`, by
  design — F05 never silently drops rows). Not a defect, but worth knowing before treating
  the "only on one board" list as pure vocabulary difference.
