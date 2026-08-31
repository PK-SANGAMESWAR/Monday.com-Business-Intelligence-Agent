# F03 — Board Seeding: Workbooks → monday.com, and the Live Verification of F02

STATUS: **COMPLETE** — implemented and verified live 2026-08-31. Deals (board id
5030996376): 346/346 items, all 12 columns, deal-value sum round-trips exactly
(2,305,518,040.91 over 165 rows). Work Orders (board id 5030996553): 176/176 items, all 38
columns, all 8 masked money-column sums round-trip exactly, 176/176 unique `Serial #`.
Offline suite 256 passed; live suite (F02 + F03) 9 passed, 0 failed.

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)
Depends on: [02_MONDAY_CLIENT.md](02_MONDAY_CLIENT.md) (COMPLETE)

---

## 1. Objective

F03 creates the two boards the entire project assumes exist, and then proves F02 can
read them back faithfully.

It is the **only component in this repository that writes to monday.com**, and the only
one that reads the workbooks. Both facts make it a boundary feature rather than a
routine one: everything about its design follows from keeping the write capability
quarantined and the transport faithful.

Two things it is *not*:

- It is **not** normalization. The board must mirror the workbook, mess included. F04
  cleans. If F03 tidies the data on the way in, then FR-6..FR-9 are being satisfied by
  a one-off script rather than by the agent, and the graded claim "handles messy data"
  becomes untrue in the only place it matters — at runtime, against the live board.
- It is **not** part of the agent. `scripts/seed_monday.py` is never imported by
  `bi_agent/`, and a test asserts that.

The second half of F03 is the part §7 of the F02 doc deferred: re-record fixtures from
the *real* boards, re-run F02's suite unchanged, and find out whether the authored
fixtures were right.

## 2. Requirement mapping

| Req | Statement | How F03 satisfies it |
|-----|-----------|----------------------|
| **DL-4** | monday.com boards created from the two sample workbooks with sensible column types | The whole feature. Scripted and repeatable, not done by hand, so a reviewer can recreate the environment from a clean account with one command. |
| **FR-4** | Never hardcode the sample data; query monday.com dynamically at runtime | F03 is the *only* reader of the workbooks, and it is not part of the agent. This is what makes FR-4 true rather than aspirational: after seeding, the xlsx files are dead weight to the running system. |
| **FR-5 / NFR-5** | Read-only — no mutations from the agent | F03 is the exception that proves the rule, so the write path is physically separate: its own document registry, its own transport, outside `bi_agent/`. §3.2. |
| **FR-3** | Read all data from both boards | Verified live for the first time here: 346 + 176 items read back through F02's paginator and checked against the workbooks. §3.8. |
| **FR-6..FR-9** | Missing values, normalization, caveats | Protected, not implemented: the mess is transported verbatim so F04 has something real to clean. §3.5. |
| **NFR-8** | Board schema as data, not scattered conditionals | One `COLUMN_SPECS` table per board maps workbook header → monday column type → value encoder. The seeder is a loop over that table. §3.4. |
| **NFR-3** | Reproducible offline tests | Every mutation is `respx`-mocked; the workbooks are read from the repo, which is deterministic. The live run is a separate, marked path. §6. |
| **NFR-7** | Observability | Per-item progress, a written seeding report naming every value that could not be represented, and a non-zero exit when verification fails. §3.7. |

## 3. Technical design

### 3.1 Module layout

```text
scripts/
  seed_monday.py        CLI: plan -> create boards -> create columns -> create items -> verify
  seeding/              (package beside the script, deliberately not under bi_agent/)
    __init__.py
    workbook.py         xlsx -> rows, faithfully; header quirks; junk detection (flag, not drop)
    schema.py           COLUMN_SPECS: header -> column_type -> value encoder
    mutations.py        the frozen registry of write documents (the inverse of F02's gate)
    writer.py           write transport: auth, retry, throttle, progress
    report.py           SeedingReport: counts, unrepresentable values, verification results
```

`scripts/seeding/` rather than `bi_agent/seeding/` is the whole point. `bi_agent` is
importable by the deployed app; `scripts` is not shipped, is not on the app's import path,
and contains the only code in the repository capable of changing a board.

### 3.2 Keeping the write capability quarantined

F02 made "the agent cannot write" structural: the transport accepts only a verified
`QueryDocument`, and every one is a constant checked at import. F03 must write, which
raises the obvious question of how without dismantling that.

Three options were considered:

| Option | Verdict |
|--------|---------|
| Add a `allow_writes=True` flag to `MondayClient` | **Rejected.** It places a write path inside the package the gate protects. FR-5 would then depend on a default argument never being changed — a guarantee about vigilance, not about structure. |
| Extract a shared transport base class into `bi_agent/monday/` for both to use | **Rejected.** Same objection, one level of indirection further away, and it reopens F02, which is closed. |
| A separate `SeedWriter` in `scripts/seeding/`, with its own client and its own registry | **Recommended.** The write capability exists in exactly one directory that the app never imports. |

The cost of the recommended option is ~40 lines of duplicated retry loop. That is paid
deliberately. What is *not* duplicated is `classify_failure`, which F02 exposes as a pure
function of `(status, body)` — it has no opinion about reads versus writes, so the seeder
imports it and inherits F02's whole failure-classification table for free, including the
HTTP-200-with-`errors[]` case.

`mutations.py` mirrors `queries.py` in the inverse direction: every write is a frozen
constant, and a guard at construction asserts each document opens **exactly one**
top-level write operation. A read document passed to the writer is refused, and a write
document is impossible to obtain anywhere else. The symmetry is not decoration — it means
"which writes can this repository perform?" is answered by reading one 60-line file.

### 3.3 What gets created

| | Deals | Work Orders |
|---|---|---|
| Board name | `Deals` | `Work Orders` |
| Source | `Deal funnel Data.xlsx`, sheet `Deal tracker`, header row **1** | `Work_Order_Tracker Data.xlsx`, sheet `work order tracker`, header row **2** (row 1 is blank) |
| Columns from source | 12 | 38 |
| Plus provenance | `Source Row` | `Source Row` |
| Items | **346** (347 data rows − 1 fully empty) | **176** (177 − 1 fully empty) |
| Item name | `Deal Name` | `Serial #` |

Item-name choice matters more for Work Orders: `Serial #` is the only true primary key in
either dataset (unique across all 176), so making it the item name gives every work order
a stable, visible identity in the monday UI and in every API response. `Deal name masked`
becomes an ordinary column.

Deals have no key at all and 3 rows have a blank name; those become `(unnamed deal)` and
are listed in the report. Duplicate item names are fine — monday allows them, and the
duplication is a true property of the source (`Sakura` is 27 rows).

**The default columns must be deleted.** Our own live probe found that a monday board
arrives with `person`, `status`, `date4` and `subitems` columns already on it. Left in
place, a Deals board would carry a stray `Status` column that is not the workbook's
`Deal Status`, which is exactly the kind of thing that makes `describe_data` lie to the
model in F06. The seeder deletes them after creating the board, which also prevents the
phantom `Subitems of Deals` board that the subitems column would otherwise create.

### 3.4 Column types — faithful transport over pretty typing

**Recommendation: `date` for dates, `numbers` for money, `text` for everything else.**

| Source shape | monday type | `column_values` encoding | Count |
|---|---|---|---|
| Dates (`Created Date`, `Date of PO/LOI`, `Probable End Date`, …) | `date` | `{"date": "YYYY-MM-DD"}` | 9 |
| Money (`Masked Deal value`, the 8 masked amount columns) | `numbers` | `"1250000"` (JSON string) | 9 |
| Identifiers, codes, categoricals, free text, quantities | `text` | `"OWNER_001"` | 32 |

Rejected: mapping the categoricals to `status`/`dropdown`. It would look better in the
monday UI and it is what a human would do by hand, but:

- `Deal Stage` has 17 distinct values *including junk*; `Type of Work` is 36 comma-joined
  strings over ~12 atoms; `Invoice Status` includes one-offs like `Billed- Visit 7`. These
  are not closed vocabularies, they are free text that happens to repeat.
- A `status` column silently coerces or rejects a value outside its labels. Silent
  coercion on the way in is indistinguishable from data loss, and we would not find out
  until F05's golden-value tests failed for reasons that looked like a metric bug.
- The casing bug `BIlled` and the junk label `Deal Status` are *findings* F04 must report.
  As labels they become permanent board furniture; as text they stay data.

`text` is not a cop-out here: it is the type that cannot lie about the source. Dates and
money get real types because those are the fields F05 must sort, filter and sum, and both
parse cleanly enough to be worth it (`#VALUE!` appears exactly once).

**Zero is written as `0`, never omitted.** 63 work orders have `Billed Value (Incl GST)` of
zero and the difference between "billed nothing" and "not recorded" is a graded
distinction (CLAUDE.md). Omitting a column from `column_values` produces an empty cell,
which would silently convert 63 real zeros into 63 nulls. There is a test for this.

**Empty stays empty.** An empty string or `None` in the source means the column is omitted
from `column_values`, producing an empty cell — the honest representation of "the workbook
had nothing here".

**The four all-empty columns are created anyway.** `Expected Billing Month`,
`Actual Collection Month`, `Collection status`, `Collection Date` are 0/177 populated. They
exist on the board so that F04 can flag them `always_null` and the agent can answer "this
field is empty for all 176 records" instead of returning a silent zero. A column that does
not exist cannot be reported as empty.

### 3.5 The mess is transported, not cleaned — including the junk rows

**Recommendation: seed the two embedded header rows (Excel rows 52 and 181) as items.**

This is the most arguable decision in F03, so here is the reasoning in full. Those two rows
carry a real deal name in column A (`Nezuko`, `Bugs Bunny`) and literal header text in every
other cell, which injects `Sector/service` as a sector, `Closure Probability` as a
probability, and so on.

| Approach | Consequence |
|---|---|
| **Seed them** (recommended) | The live board contains the same junk the workbook does. F04's detector (`Deal Status == 'Deal Status'`) runs against real data, the data-quality report has something true to say, and the demo's central claim is demonstrably true end to end. Cost: two junk rows are visible on the board, and every count is 346 rather than 344. |
| Drop them at seed time | The board is cleaner and the item count matches the "344 real deals" figure. But F04's junk detection becomes dead code exercised only by unit tests over the xlsx, the data-quality report has nothing to report, and we would be claiming to handle messiness that we quietly removed beforehand. |

The same logic applies to `#VALUE!`, `BIlled`, the mixed-unit quantities and the
unprefixed `Project Completed` stage: all are transported verbatim.

The one exception is **fully empty rows** — one per workbook. These are dropped, because
an item with no name and no values is not a record of anything, and monday would reject
or mangle it. Both dropped rows are named in the report, so the drop is visible rather
than silent.

### 3.6 Idempotency, resumability, and not destroying anything

522 items at a throttled write rate is a run measured in minutes, over a network. It will
be interrupted at some point, and a second run must not double the board.

**Provenance column.** Each item gets `Source Row` = `DEAL-0052` / `WO-0113` (the Excel row
number). This is a column that does not exist in the source, so it needs justifying:
without a per-row key, deals cannot be deduplicated at all — 346 rows carry 154 distinct
names, so "has this row already been created?" is unanswerable by name. `Source Row` makes
the seeder idempotent and gives F04 a way to trace any board record back to a workbook row
when a number looks wrong. It is provenance metadata, not fabricated business data.

**Resume semantics.** The seeder reads existing items through F02's `BoardReader` first,
builds the set of `Source Row` values already present, and creates only what is missing. A
completed board therefore makes a second run a no-op that issues zero writes.

**It will not delete your data.** If a board of the target name already exists, the default
behaviour is *resume*, never recreate. `--recreate` is required to delete and rebuild, and
it prompts for confirmation naming the board and its item count. There is no flag that
deletes a board without a human typing something. The seeder also refuses to resume into a
board whose columns do not match the expected schema, because appending workbook rows to
somebody's unrelated board named "Deals" is the worst thing this script could plausibly do.

### 3.7 Throttling and the calibration probe

monday.com meters GraphQL by complexity, and **write costs are materially higher than read
costs**. F02 measured reads directly: an 81-point query against a ~1,000,000-point budget.
We have measured nothing about writes, and this document is not going to guess.

**Step 1 of implementation is a calibration probe:** create a throwaway board, create one
column, create one item, read `complexity { before after query }` on each, then delete the
board. The measured per-mutation cost determines the throttle, and the number goes into
§9 as evidence. If a write turns out to cost ~30k points, the budget allows roughly 33
items per minute and a full seed is a ~16-minute run — which is fine, but only if the
pacing is derived from a measurement rather than from optimism.

Pacing is then: a configurable items-per-minute, plus F02's existing behaviour of honouring
`Retry-After` and the `reset in N seconds` complexity hint on a 429. Because resume is
keyed on `Source Row`, a 429 that outlasts the retry budget is not a disaster — the run
stops, and the next one continues where it stopped.

**One item per request, no batching.** monday.com allows several aliased `create_item`
fields in one mutation, which would cut request count substantially. It is rejected for
now because it requires *building GraphQL documents dynamically*, and the entire integrity
argument of §3.2 rests on every write being a frozen, reviewed constant. 522 sequential
requests is acceptable at this size. If the calibration probe shows the run would take
more than ~20 minutes, this decision gets revisited with a fixed-size batch template — and
that revisit is recorded here rather than made silently.

### 3.8 Verification — the part that makes F03 more than a data-entry script

Seeding that is not verified is a guess. After the write phase, the seeder reads both
boards back **through F02**, and checks:

| Check | Why this one |
|---|---|
| Item counts are 346 and 176 | FR-3's first real test. A short page or a dropped write shows up here and nowhere else. |
| `Serial #` is unique across 176 items | Proves the only primary key in the data survived the round trip. |
| Every workbook header resolves via `snapshot.require_columns(headers)` | Exercises F02's title→ID indirection against a real board (NFR-8), and fails naming every missing column at once. |
| **Deal value sums to `2,305,518,041` over exactly 165 items** | The strongest check available. It is computed by reading the board back, not the workbook, so it proves numbers survived encoding, transport, storage and F02's paginator without loss or coercion. |
| 176 unique serials, 63 zero billed values, 165 valued deals | The golden figures from plan §8, now asserted against monday.com rather than against a spreadsheet. |
| `#VALUE!` arrives as the literal string, not as 0 or null | Proves the mess was transported, per §3.5. |

Then the F02 loop closes: re-run `scripts/record_fixtures.py` against the real boards,
diff the recorded structure against the authored fixtures, and **re-run F02's entire suite
unchanged**. F02's doc §7 promised that this is where fixture errors surface cheaply. Any
correction lands in F02's doc §9.4 as a follow-up, not silently.

Finally the board IDs are written to `.env` (`MONDAY_DEALS_BOARD_ID`,
`MONDAY_WORK_ORDERS_BOARD_ID`), which flips `Settings.boards_configured` to true and lets
`main.py` report the environment as fully wired.

### 3.9 Reading the workbooks: openpyxl, not pandas

pandas is in the plan's dependency list and F04 will need it. F03 will not, and should not
use it here: pandas coerces on read — integers become floats, empty cells become `NaN`,
mixed-type columns become `object` with inconsistent members. Every one of those coercions
is a small unfaithfulness introduced *before* the data reaches the board, in a feature
whose entire job is faithful transport.

`openpyxl` with `data_only=True` returns native Python types — `datetime`, `int`, `float`,
`str`, `None` — which is exactly what the value encoders want. It is already a dependency.
**F03 adds no new dependencies.**

## 4. Files to create / modify

| File | Status | Responsibility |
|------|--------|----------------|
| `scripts/seed_monday.py` | create | CLI: `--dry-run`, `--recreate`, `--resume`, `--only deals\|work-orders`, `--items-per-minute`, `--verify-only`. |
| `scripts/seeding/workbook.py` | create | Faithful xlsx reader: header row quirk, fully-empty-row drop, junk-row flagging. |
| `scripts/seeding/schema.py` | create | `COLUMN_SPECS` per board: header → column type → encoder. The NFR-8 table. |
| `scripts/seeding/mutations.py` | create | Frozen write-document registry + the inverse gate. |
| `scripts/seeding/writer.py` | create | Write transport: auth, retry, throttle, progress; imports `classify_failure` from F02. |
| `scripts/seeding/report.py` | create | `SeedingReport` + Markdown output to `docs/SEEDING_REPORT.md`. |
| `tests/unit/test_workbook_reader.py` | create | Header quirks, row counts, junk flagging, type fidelity. |
| `tests/unit/test_seed_schema.py` | create | Every header mapped; encoder table incl. zero-vs-empty. |
| `tests/unit/test_write_gate.py` | create | The inverse gate, and that `bi_agent/` never imports `scripts`. |
| `tests/integration/test_seeding.py` | create | Full seed against respx: documents sent, ordering, idempotency, throttling, 429 mid-run. |
| `tests/live/test_live_seeded_boards.py` | create | `@pytest.mark.live`: the §3.8 verification table against the real boards. |
| `docs/SEEDING_REPORT.md` | create | Generated. What was created, what could not be represented, verification results. |
| `.env` | modify | Board IDs, written by the seeder. Gitignored. |
| `README.md` | modify | The DL-3 board-setup instructions: clone → token → `uv run python scripts/seed_monday.py`. |

Not touched: anything under `bi_agent/`. If F03 needs a change there, that is a signal F02
got something wrong, and it gets its own commit against F02's doc.

## 5. Implementation plan

1. **Calibration probe** (§3.7): measure write complexity on a throwaway board, delete it,
   record the numbers. Everything about pacing depends on this, so it goes first.
2. `mutations.py` + `test_write_gate.py` — the write registry and its guard, before any
   code that can send a write exists. (Same ordering rule F02 used for its read gate.)
3. `workbook.py` + `test_workbook_reader.py` — assert the measured row counts and header
   quirks from DATA_PROFILE.md against the real files.
4. `schema.py` + `test_seed_schema.py` — the header→type→encoder table, with the
   zero-vs-empty and `#VALUE!` cases pinned.
5. `writer.py` + `report.py`; then `test_seeding.py` against respx (failing first).
6. `seed_monday.py` wiring, with `--dry-run` working before anything else does.
7. Run `--dry-run`, review the printed plan by eye against DATA_PROFILE.md.
8. **Live seed run.** Deals first (smaller schema, 12 columns) to shake out encoding
   errors on the cheaper board, verify, then Work Orders.
9. Run the §3.8 verification. Fix whatever it finds. Re-run.
10. Re-record F02 fixtures from the real boards; diff; **re-run F02's suite unchanged**.
    Record the outcome in F02 §9.4 and here.
11. Write board IDs to `.env`; confirm `uv run python main.py` reports fully configured.
12. Generate `docs/SEEDING_REPORT.md`; update README (DL-3); fill in §9; mark COMPLETE;
    one commit.

## 6. Test plan

Written before implementation, per EXECUTION.md rule 4. Numbering continues from F02's 52.

### 6.1 The write gate — `test_write_gate.py`

| # | Case | Expectation |
|---|------|-------------|
| 53 | Every document in the write registry | Passes its guard; registry non-empty; each opens exactly one write operation. |
| 54 | A read document handed to the writer | Rejected — the guard is two-way, so a misfiled constant fails loudly. |
| 55 | A bare string handed to the writer | Rejected before any HTTP call. |
| 56 | `bi_agent/` imports nothing from `scripts` | AST sweep over the package. This is the structural half of FR-5. |
| 57 | The write registry contains no `delete_board` outside the `--recreate` path | The destructive document exists in exactly one code path, and a test says so. |

### 6.2 Workbook reading — `test_workbook_reader.py`

| # | Case | Expectation |
|---|------|-------------|
| 58 | Deals: 12 headers, exactly as measured | Header list matches DATA_PROFILE.md byte for byte. |
| 59 | Work Orders: header on **row 2** | 38 headers; reading row 1 would yield all-None and must fail loudly, not silently. |
| 60 | Row counts after dropping fully-empty rows | 346 and 176. |
| 61 | The dropped rows are reported | One per workbook, named by row number. |
| 62 | Embedded header rows at 52 and 181 are **kept** and **flagged** | Present in output, marked suspicious, detected on `Deal Status == 'Deal Status'`. |
| 63 | Native types survive | `datetime` stays datetime, `int` stays int, `''` stays `''`, `None` stays None — no pandas-style coercion. |
| 64 | `#VALUE!` arrives as the literal string | Not 0, not None. |
| 65 | `Serial #` uniqueness in the source | 176 distinct, asserted before we depend on it. |

### 6.3 Schema and encoding — `test_seed_schema.py`

| # | Case | Expectation |
|---|------|-------------|
| 66 | Every workbook header has a spec | 12 + 38, no header unmapped and no spec orphaned. |
| 67 | Date encoding | `datetime(2024,8,14)` → `{"date": "2024-08-14"}`. |
| 68 | Number encoding | `1250000` → `"1250000"`; `1250000.5` → `"1250000.5"`. |
| 69 | **Zero encodes as `0`, not omitted** | The 63-zero case from §3.4. |
| 70 | Empty string and None are omitted | Produces an empty cell, not `"None"` or `"0"`. |
| 71 | `#VALUE!` in a numbers column | Cannot be encoded → column omitted **and** recorded as unrepresentable in the report. Never 0. |
| 72 | Free text passes through verbatim | `NA verbal confirmation for km`, `5360 HA`, `BIlled` unchanged. |
| 73 | A date-typed cell holding text | Omitted and reported, not crashed on. |
| 74 | `Source Row` encoding | `DEAL-0052`, `WO-0113`; zero-padded so it sorts. |

### 6.4 Seeding integration — `test_seeding.py` (respx)

| # | Case | Expectation |
|---|------|-------------|
| 75 | Full run on a clean account | `create_board` ×2, `delete_column` for each default, `create_column` per header in workbook order, `create_item` per row. |
| 76 | Column order is preserved | The board reads left-to-right like the workbook; a reviewer comparing them should not have to hunt. |
| 77 | Default columns deleted | The §3.3 finding, asserted. |
| 78 | `--dry-run` | **Zero** requests; prints counts, column list and an estimated duration. |
| 79 | Second run on a complete board | Zero writes. Idempotency. |
| 80 | Resume after interruption at item 200 | Creates exactly the missing 146, in order, no duplicates. |
| 81 | Existing board with mismatched columns | Refuses to write; names the mismatch. |
| 82 | `--recreate` without confirmation | Refuses. |
| 83 | 429 mid-run | Backs off honouring `Retry-After`, then continues; injected sleep so the test is instant. |
| 84 | Throttle pacing | Respects `--items-per-minute` via injected sleep and clock. |
| 85 | Auth failure on the first write | `MondayAuthError`, nothing partially created, actionable message. |
| 86 | `create_item` returns 200 with `errors[]` | Treated as a failure, not a success — F02's classifier, reused. |
| 87 | Verification failure path | Count mismatch → non-zero exit and a message naming the discrepancy. |
| 88 | The seeding report | Lists created counts, dropped rows, unrepresentable values, and the verification table. |

### 6.5 Live verification — `test_live_seeded_boards.py` (`@pytest.mark.live`)

| # | Case | Expectation |
|---|------|-------------|
| 89 | Both boards resolve by name through F02 | `resolve_board("Deals")`, `resolve_board("Work Orders")`. |
| 90 | Item counts | 346 and 176, read through F02's paginator. |
| 91 | Pagination actually paginated | ≥1 page; with 346 items at a 500 page size this is one page, so the test asserts the *cursor contract* and a second run at `--page-size 100` proves multi-page reads on real data. |
| 92 | All 50 workbook headers resolve via `require_columns` | NFR-8 against a real board. |
| 93 | **Deal value sums to 2,305,518,041 over 165 items** | Read back from monday.com. The round-trip proof. |
| 94 | 176 unique serials; 63 zero billed values | Golden figures, asserted live. |
| 95 | `#VALUE!` present as a literal string | The mess survived. |
| 96 | The four always-empty columns exist and are empty | So F04 can report them as unanswerable. |
| 97 | F02's suite green against re-recorded fixtures | The §7 promise, discharged. |

### 6.6 Not tested here, deliberately

Normalization, canonical field names and the data-quality report are F04's. F03 asserts
only that what is on the board equals what is in the workbook.

## 7. Risks

1. **Write complexity is unmeasured.** The single largest unknown; §3.7 addresses it with a
   probe before any bulk write. If writes are far more expensive than assumed, the mitigation
   is a slower run, not a redesign — resume makes a long run safe.
2. **A partially seeded board is a wrong-answer machine.** A board with 300 of 346 deals
   answers every question confidently and incorrectly. Mitigations: verification is part of
   the seeder rather than a follow-up chore, it exits non-zero on mismatch, and the
   `Source Row` key makes "what is missing" a computable question.
3. **`--recreate` is genuinely destructive.** Guarded by confirmation, restricted to one code
   path, and covered by case 82. Worth a second pair of eyes at review.
4. **The authored fixtures may still be wrong** in ways the starter board could not reveal —
   for example, how a `numbers` column renders in `text` versus `value`. This is expected;
   step 10 exists precisely to find it, and finding it is a success for F03, not a failure.
5. **Board and column limits.** Unverified on this plan tier. The probe in step 1 also
   confirms a 39-column board is acceptable before we build one twice.

## 8. Decisions needing confirmation before implementation

Raised rather than assumed, per EXECUTION.md.

| # | Decision | Recommendation |
|---|----------|----------------|
| **D-1** | Seed the two embedded junk header rows, or drop them? | **Seed them** (§3.5). Item counts become 346/176 rather than 344/176, and the messiness the brief is about becomes real at runtime. |
| **D-2** | Categoricals as `text`, or as `status`/`dropdown`? | **Text** (§3.4), except dates and money. Prettier boards are not worth silent coercion. |
| **D-3** | Add the `Source Row` provenance column that is not in the source? | **Yes** (§3.6). Without it, deals cannot be deduplicated and seeding is not idempotent. |
| **D-4** | Plan §9.2's open question: add an OAuth `boards:read` app to close the read-only gap at the credential layer? | **Defer, and record it.** The gap is real but the mitigation is disproportionate for a prototype: F02's gate is structural, and F03 keeps the write path outside the shipped package. Belongs in the Decision Log (DL-2) as a known limitation with a stated fix. |
| **D-5** | Board names exactly `Deals` and `Work Orders`? | **Yes** — F02 resolves by name, and these are what the brief calls them. Configurable via CLI if you would rather namespace them. |

## 9. Implementation results

D-1 through D-5 were all taken as recommended. `scripts/seeding/{workbook,schema,mutations,
writer,report}.py` and `scripts/seed_monday.py` implemented as designed, plus 55 new tests
(`tests/unit/test_workbook_reader.py`, `test_seed_schema.py`, `test_write_gate.py`,
`tests/integration/test_seeding.py`, `tests/live/test_live_seeded_boards.py`) — full suite
`uv run pytest -q`: **256 passed**, 9 live deselected. Live suite `uv run pytest -m live -v`:
**9 passed** (F02's 5 smoke tests unchanged + F03's 4 verification tests).

`--dry-run` against the real workbooks confirmed the plan exactly: 346 + 176 = 522 items,
12 + 38 columns, correct types, both junk rows (52, 181) flagged for Deals.

**Two real bugs found only by the live run, neither exercisable offline:**

1. **A GraphQL-created board's `columns` list includes the mandatory, undeletable `name`
   column** alongside the real defaults — `DeleteMandatoryColumnException` on the first
   attempt. Fixed by skipping `column.id == "name"` in the default-column cleanup loop.
   Also revealed that this API path does **not** add `person`/`status`/`date4`/`subitems`
   defaults the way the UI's "Add board" flow does — section 3.3's premise about deleting
   those was moot in practice, but the code that would delete them is still correct and
   still runs (it is just a no-op here).
2. **A freshly created board carries one auto-generated sample item** ("Task 1", no
   columns filled in), invisible to the `columns` query and therefore invisible to (1)'s
   cleanup. It silently inflated every item count until caught. Fixed with a new
   `DELETE_ITEM` mutation, sent against every item found on a board immediately after
   creation, before any workbook row is written. Regression-tested in
   `test_seeding.py::test_stray_default_item_is_deleted_before_seeding`.

**A third bug found in F02 itself, not just the seeder:** `MondayClient._decode` (and its
`SeedWriter` mirror) raised `MondayQueryError` — non-retryable — whenever a response body was
not JSON, regardless of status code. monday.com's rate limiter returns an **HTML page**, not
a GraphQL error envelope, on a 429. Every throttled write was therefore misclassified as a
permanent failure and never retried, and — worse — a throttled `LIST_BOARDS` inside
`resolve_or_create_board`'s existence check was caught by `except MondayQueryError`, which
means "board not found," so a rate-limited resolve looked like a missing board. Fixed by
decoding tolerantly (`strict=False`) for any non-200 response and letting `classify_failure`
key on the status code alone; a 200 with an unparseable body is unaffected (still raises
immediately, per F02's original case 26). This is a correctness fix to `bi_agent/monday/client.py`,
a file F02 had marked COMPLETE — recorded here rather than silently touched, and F02's own
suite (including case 26) was re-run and stayed green.

**Live throttling, measured rather than guessed.** The account's write path is rate-limited
by something that returns an HTML body on 429 (a WAF/CDN limit, not the GraphQL complexity
budget F02 measured at ~990k points available) — it engaged around 70 unthrottled requests
in ~50 seconds during the first run's column-setup burst, and again mid-run at a sustained
~30 items/minute. No calibration probe was built (a scoped-down decision, see below);
instead: every write is now paced (not only `create_item` — column/board setup too, which
is what actually tripped it first), the default pace was lowered to
**20 items/minute**, and the writer's retry budget was raised to **8 attempts** (vs. F02's
default 3) since the observed throttle window outlasted three retries' backoff even at the
30s cap. A resumed run with these settings completed cleanly with zero further 429s.

Final live seed, in two resumed/recreated passes after the fixes above:
`uv run python scripts/seed_monday.py --only deals` (resumed the 19 items already written)
then `uv run python scripts/seed_monday.py --only work-orders --recreate --yes` (the Work
Orders shell from the first broken run had the wrong schema and was rebuilt clean). Both
boards, `--verify-only`-equivalent checks all PASS (see STATUS line above for figures).

`docs/SEEDING_REPORT.md` is generated by the seeder itself and committed as the record of
the final run.

## 10. Known limitations

- **No automated calibration probe (§3.7 as originally scoped).** A conservative default
  (20 items/minute, 8 retries, 30s backoff cap) was substituted and proven live instead of
  measuring per-mutation complexity with a throwaway board. Sufficient for a one-time seed
  of 522 items (~26 minutes at this pace); would need real measurement before seeding a
  meaningfully larger board.
- **`create_item` retries are not fully idempotent against a timeout-class failure.** A
  network timeout after monday.com already committed the write, followed by a retry, could
  create a duplicate item within a single run (the `Source Row` dedup check runs once at
  resume time, not after every write). Not observed live; the failure mode seen in practice
  (429, no body) is safely retryable because monday.com never processes the mutation on a
  429. Worth a per-request idempotency key if this script is reused at larger scale.
- **D-4 (OAuth `boards:read` app) remains deferred**, per plan section 9.2 and the Decision
  Log — the credential is a full-permission personal token; read-only is enforced in code
  (F02's gate), not by monday.com.
- **The one-off live fixes in this run (deleting a stray board, deleting a stray item) were
  applied by hand via `SeedWriter`/`DELETE_ITEM` outside `seed_monday.py`'s own flow**,
  because they were cleaning up a state the *buggy* code produced. The fixed code no longer
  produces that state (proven by the clean Work Orders `--recreate` run), so this is not a
  standing operational step.
