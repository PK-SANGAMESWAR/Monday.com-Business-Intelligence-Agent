# F02 — monday.com Client: Transport, Read-Only Gate, Board Fetch

STATUS: **COMPLETE** — implemented and verified 2026-08-31. Offline suite green
(199 passed, 5 live deselected); live smoke tests green against the real account.
Scope claim per §7: correct against the documented API **and the real envelope**,
not against the real Deals and Work Orders boards, which do not exist until F03.

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)
Depends on: [01_CONFIG_SKELETON.md](01_CONFIG_SKELETON.md) (COMPLETE)

---

## 1. Objective

Everything above this layer assumes it can say "give me the Deals board" and receive rows.
F02 is what makes that true: an authenticated GraphQL client that resolves boards and
columns by *name*, pages through every item, converts every failure mode into one of F01's
typed errors, caches results with a TTL, and **cannot issue a mutation**.

It ships no business logic and no normalization — it returns monday.com's payload shape,
not our canonical fields. F04 owns the translation. The boundary matters: if F02 starts
knowing what a "deal value" is, the schema ends up split across two layers.

The one thing F02 cannot do yet is prove itself against the real boards, because they do
not exist. §7 addresses that head-on rather than pretending otherwise.

## 2. Requirement mapping

| Req | Statement | How F02 satisfies it |
|-----|-----------|----------------------|
| **FR-1** | Connect to monday.com via MCP or API | Direct GraphQL API v2 over `httpx`, per plan §3.5 / OQ-6. |
| **FR-2** | Handle authentication and connection management | The connection half (F01 was the auth half): token from `Settings` into the `Authorization` header, pinned `API-Version`, one reused connection pool, explicit timeouts. |
| **FR-3** | Read all data from both boards | Cursor pagination over `items_page` until exhausted, plus column introspection. "All" is asserted by a test, not assumed — a short page must not silently truncate a board. |
| **FR-4** | Never hardcode CSV data; query dynamically at runtime | The client is the only source of board data. The workbooks are never read by the agent package; test fixtures are recorded API responses, not spreadsheet rows. |
| **FR-5** | Read-only — no mutations | Two independent mechanisms, §3.3. `ReadOnlyViolationError` already exists from F01. |
| **FR-16** | Graceful handling of API failures | Failure classification (§3.5) → F01's typed errors → retry or stale-cache degradation (§3.7), each with a test proving the *degradation*, not just the raise. |
| **NFR-1** | Latency | Fetch-once per TTL window; ~520 rows over 2 boards is 2-4 requests, not one per question. |
| **NFR-3** | Offline tests | `respx` mocks the transport. No test touches the network unless marked `live`. |
| **NFR-5** | Read-only safety | Enforced before the request leaves the process, §3.3. |
| **NFR-6** | Cost | TTL cache, §3.6. One board fetch per window. |
| **NFR-7** | Observability | Every request logs method, board, page count, duration, and complexity spend under F01's `request_id`. |
| **NFR-8** | Maintainability | Column IDs resolved by title at runtime; no opaque `text_mkq1abc` literal anywhere. |

## 3. Technical design

### 3.1 Module layout

```text
bi_agent/monday/
  __init__.py      public surface: MondayClient, BoardReader, BoardSnapshot
  queries.py       the frozen registry of GraphQL documents we are allowed to send
  client.py        transport: auth, timeouts, retry/backoff, failure classification
  boards.py        board + column resolution, cursor pagination, TTL cache
scripts/
  record_fixtures.py   live -> tests/fixtures/*.json  (not importable from bi_agent)
```

`client.py` knows about HTTP and errors. `boards.py` knows about boards, columns and
cursors. Nothing in either knows what a deal is.

### 3.2 Public interface

```text
MondayClient(settings)
  .execute(document, variables=None) -> dict     # returns response["data"]
  .close()                                       # context manager too

BoardReader(client)
  .resolve_board(name_or_id)  -> BoardRef(id, name)
  .fetch_columns(board_id)    -> list[Column(id, title, type)]
  .fetch_items(board_id, *, force_refresh=False) -> BoardSnapshot
  .invalidate(board_id=None)

BoardSnapshot(
    board_id, board_name, columns, items,
    fetched_at, source,      # "live" | "cache" | "stale-cache"
    page_count, item_count,
)
```

`source` and `fetched_at` are not decoration. Plan §4.3 requires the agent to serve stale
data *and say it is stale*; that is only possible if staleness survives the call boundary
as data. A client that quietly returns old rows is the failure this field prevents.

### 3.3 The read-only gate (FR-5, NFR-5)

Two independent mechanisms, because this is a hard constraint and the token is a personal
**admin** token with full write permissions (plan §9.2) — the credential grants no
protection whatsoever.

**Mechanism 1 — a frozen registry.** `execute()` accepts only a `QueryDocument` obtained
from `queries.py`, never a bare string. Every document is defined at import time in one
module of perhaps five constants. There is no code path that sends a caller-supplied
string, so "the agent decided to write" is not expressible.

**Mechanism 2 — lexical verification.** Each registry entry is verified at construction:
comments and string literals are stripped, then every top-level operation keyword must be
`query`. `mutation` or `subscription` anywhere raises `ReadOnlyViolationError` at **import
time**, so a bad document fails the test suite rather than a user's session.

Considered and rejected: parsing with `graphql-core`. It is more rigorous than a lexical
check, but it is a new dependency whose entire value here is validating five hand-written
constants that a human reviews. Mechanism 1 already makes mechanism 2 defence in depth.
Recorded here so the trade-off is deliberate; if the document set ever grows or becomes
dynamic, this decision should be revisited.

The gate is tested adversarially: `mutation` inside a comment, inside a string literal, as
a field alias, as a substring of a field name (`mutationCount`), lowercase/mixed case, and
after leading whitespace and newlines. A gate that only catches `"mutation {"` is theatre.

### 3.4 GraphQL documents

Five, and no more:

| Name | Purpose |
|------|---------|
| `ME` | `{ me { id name is_admin } }` — connectivity and token check. |
| `LIST_BOARDS` | Board id + name, so a board resolves by name before F03 records its ID. |
| `BOARD_COLUMNS` | `columns { id title type }` for title→ID indirection. |
| `BOARD_ITEMS_FIRST` | `items_page(limit:)` — first page, returns `cursor` + items. |
| `BOARD_ITEMS_NEXT` | `next_items_page(limit:, cursor:)` — subsequent pages. |

Item fields requested: `id`, `name`, and `column_values { id type text value }`. Both
`text` (display string) and `value` (raw JSON) are fetched: `text` is what the workbooks
show, `value` preserves type information that F04 needs for dates and numbers. Fetching
one and regretting it later would mean re-recording every fixture.

`complexity { before after }` is included on item queries so spend is logged (NFR-7).
Budget is ~990k points against a cost measured in hundreds — this is observability, not a
constraint we expect to bind.

### 3.5 Failure classification

The mapping from what comes back to F01's typed errors. monday.com's awkward habit is
returning **HTTP 200 with an `errors[]` array**, so status code alone is not enough.

| Observed | Error raised | Retry? |
|----------|--------------|--------|
| 401, or 200 with an authentication error | `MondayAuthError` | No — retrying a bad token wastes time and says nothing new |
| 429, or a complexity/rate-limit error in `errors[]` | `MondayRateLimitError(retry_after)` | Yes, honouring `Retry-After` when present |
| 5xx | `MondayUnavailableError` | Yes |
| Timeout, connection error, DNS | `MondayUnavailableError` | Yes |
| 200 with other `errors[]` | `MondayQueryError` | No — a malformed query fails identically on retry |
| 200, `data` absent or not a dict | `MondayQueryError` | No |
| Requested board absent from response | `MondayQueryError` | No |
| Expected column title absent | `SchemaMismatchError(missing=[...])` | No |

**Assumption flagged for live verification:** exact auth and rate-limit payload shapes are
taken from monday.com's documented behaviour, not yet observed. The classifier is
therefore written to key on *both* HTTP status and error-message content, and the live
smoke tests in §6.3 exist specifically to confirm or correct this. If reality differs, the
classifier changes — nothing above it does.

### 3.6 TTL cache and its boundary with F04

`BoardReader` caches the **raw** payload per board with `cache_ttl_seconds` (default 300).
F04's repository will separately cache the **normalized** DataFrame.

That looks like duplication, so the reason is worth stating: the raw cache exists to serve
FR-16 degradation. When monday.com is down, we need the last-known *rows*; re-normalizing
them is cheap and deterministic. Coupling the two caches would mean a normalization bug
invalidates our only copy of data we can no longer re-fetch.

On a failure with a cache entry **past** its TTL, `fetch_items` returns it with
`source="stale-cache"` rather than raising. Serving stale data labelled as stale beats
failing, and beats serving it silently. If there is no entry at all, the error propagates
and F06 degrades per plan §4.3.

### 3.7 Retry and backoff

Exponential with full jitter: `min(2^attempt, cap)` seconds randomized, `max_retries`
attempts (default 3), `Retry-After` honoured when the API supplies it.

**`tenacity` is dropped.** Plan §5 listed it provisionally. The retry predicate here is
"retry exactly the two error classes our own classifier produced", which is a two-line
condition; tenacity's value is decorator ergonomics and policy composition we would not
use. Roughly 25 lines of explicit loop is more testable — the sleep is injectable, so the
backoff tests run instantly and deterministically rather than actually sleeping.

### 3.8 Board and column resolution (NFR-8, plan §2.4)

Board IDs are optional in `Settings` and do not exist until F03. So `resolve_board`
accepts a name or an ID: given an ID it uses it; given a name it lists active boards and
matches case-insensitively, raising `MondayQueryError` naming the boards it *did* find if
there is no match. That message is the one a user sees when seeding has not run, so it
must be specific.

Column resolution builds a `{title: column_id}` map per board, cached with the snapshot.
Callers ask for titles; opaque IDs never leave this layer. `require_columns(titles)` raises
`SchemaMismatchError(missing=[...])` listing every absent title at once — F01 already
makes that list part of the user-facing message.

### 3.9 Observability

Every request logs under a per-request `request_id` (uuid4, 8 hex chars) — the field F01
put in the log format for exactly this. One INFO line per completed fetch (board, pages,
items, ms, complexity spent), one WARNING per retry with the reason, one ERROR per
terminal failure. The token cannot appear: `SecretRedactionFilter` receives it via
`configure_logging`, per F01 §9's first limitation.

## 4. Files to create / modify

| File | Status | Responsibility |
|------|--------|----------------|
| `bi_agent/monday/__init__.py` | create | Public surface. |
| `bi_agent/monday/queries.py` | create | Frozen document registry + import-time read-only verification. |
| `bi_agent/monday/client.py` | create | Transport, auth, retry, failure classification. |
| `bi_agent/monday/boards.py` | create | Board/column resolution, pagination, TTL cache, `BoardSnapshot`. |
| `scripts/record_fixtures.py` | create | Live → `tests/fixtures/`. Read-only; outside the agent package. |
| `tests/fixtures/*.json` | create | Recorded/authored API responses. |
| `tests/unit/test_read_only_gate.py` | create | Adversarial mutation-detection cases. |
| `tests/unit/test_queries.py` | create | Registry integrity. |
| `tests/integration/test_client.py` | create | Transport, retries, failure mapping (respx). |
| `tests/integration/test_boards.py` | create | Pagination, resolution, cache, staleness. |
| `tests/live/test_live_smoke.py` | create | `@pytest.mark.live`, deselected by default. |
| `pyproject.toml` | modify | Add `httpx`; dev `respx`. |
| `tests/conftest.py` | modify | Fixture loader, injectable clock/sleep, `settings` factory. |

Not touched: `bi_agent/config.py`, `errors.py`, `logging_config.py` — F01 is closed. If
F02 needs a change there, that is a signal F01 got something wrong and it gets its own
commit.

## 5. Implementation plan

1. Add `httpx` + `respx`; create the `monday/` package and `tests/integration/` layout.
2. Write `queries.py` and its read-only verification, plus `test_read_only_gate.py` —
   **this goes first**, so no code that can send a request exists before the gate does.
3. Author fixtures from the documented response shapes.
4. Write the integration tests against those fixtures (failing).
5. Implement `client.py`: transport → classification → retry.
6. Implement `boards.py`: resolution → pagination → cache → snapshot.
7. Run the suite; fix; re-run until green.
8. Write `scripts/record_fixtures.py`; run it live against the account's existing starter
   board to validate the real envelope shape (§7).
9. Reconcile authored fixtures against the recorded shape; re-run the suite.
10. Record actual output in §8, mark COMPLETE, commit.

## 6. Test plan

Written before implementation, per EXECUTION.md rule 4.

### 6.1 Read-only gate — `test_read_only_gate.py`

| # | Case | Expectation |
|---|------|-------------|
| 1 | Every document in the registry | Passes verification; the registry is non-empty. |
| 2 | `mutation { create_item ... }` | `ReadOnlyViolationError`. |
| 3 | `MUTATION` / `MuTaTiOn` | Rejected — case must not be an escape. |
| 4 | Leading whitespace/newlines before `mutation` | Rejected. |
| 5 | Named mutation `mutation Foo { ... }` | Rejected. |
| 6 | `subscription { ... }` | Rejected — not a read either. |
| 7 | `mutation` inside a `#` comment, document otherwise a query | **Accepted** — no false positive. |
| 8 | `mutation` inside a string literal argument | **Accepted**. |
| 9 | Field named `mutationCount` | **Accepted** — substring matching is not enough. |
| 10 | Multi-operation document, second operation a mutation | Rejected. |
| 11 | `execute()` given a bare string instead of a registry document | Rejected before any HTTP call. |
| 12 | Whole-suite sweep: no `mutation` literal anywhere in `bi_agent/` | Guards against a future addition bypassing the registry. |

### 6.2 Transport and failure mapping — `test_client.py` (respx)

| # | Case | Expectation |
|---|------|-------------|
| 13 | Successful 200 | Returns `data`; `Authorization` and `API-Version` headers correct. |
| 14 | Token never logged | Full request cycle at DEBUG; token absent from the stream. |
| 15 | 401 | `MondayAuthError`; **exactly one** request attempted. |
| 16 | 200 + auth error in `errors[]` | `MondayAuthError` — the 200-with-errors case. |
| 17 | 429 with `Retry-After: 5` | `MondayRateLimitError.retry_after == 5`; backoff waits accordingly. |
| 18 | 429 then 200 | Succeeds on retry; one warning logged. |
| 19 | 500 ×3 then 200 | Succeeds; exactly 3 retries. |
| 20 | 500 always | `MondayUnavailableError` after `max_retries`, not an infinite loop. |
| 21 | `max_retries=0` | Fails on first attempt; setting is honoured. |
| 22 | Timeout | `MondayUnavailableError`; retried. |
| 23 | Connection error | `MondayUnavailableError`; retried. |
| 24 | 200 + non-auth `errors[]` | `MondayQueryError`; **not** retried. |
| 25 | 200 + malformed body (no `data`) | `MondayQueryError`, not `KeyError`. |
| 26 | 200 + invalid JSON | `MondayQueryError`, not a JSON traceback. |
| 27 | Backoff timing | Delays grow exponentially and are jittered; injected sleep, so the test is instant. |
| 28 | Error bodies echoing request context | Redacted in logs. |
| 29 | Client closes its connection pool | No leaked transport after context exit. |

### 6.3 Boards, pagination, cache — `test_boards.py`

| # | Case | Expectation |
|---|------|-------------|
| 30 | Single-page board | All items returned; `page_count == 1`. |
| 31 | Three-page board via cursor | **All** items, in order, no duplicates, no drops. The FR-3 test. |
| 32 | Final page returns `cursor: null` | Loop terminates. |
| 33 | Empty board | Empty snapshot, no error. |
| 34 | Runaway cursor (server keeps returning one) | Page cap raises rather than looping forever. |
| 35 | Resolve board by name, case-insensitive | Correct ID. |
| 36 | Resolve by ID when `Settings` has one | No `LIST_BOARDS` call. |
| 37 | Board name not found | `MondayQueryError` naming the boards that *do* exist. |
| 38 | Duplicate board names | Deterministic, and the ambiguity is reported. |
| 39 | Column title → ID map | Titles resolve; no opaque ID escapes the layer. |
| 40 | `require_columns` with 2 of 3 absent | `SchemaMismatchError.missing` lists **both**. |
| 41 | Second fetch inside TTL | Served from cache; zero HTTP calls; `source == "cache"`. |
| 42 | Fetch after TTL expiry | Re-fetched; `source == "live"`. Clock injected. |
| 43 | `force_refresh=True` | Bypasses a valid cache entry. |
| 44 | API fails, stale entry present | Returns it, `source == "stale-cache"`, `fetched_at` preserved. |
| 45 | API fails, no cache entry | Error propagates. |
| 46 | `invalidate()` | Next call re-fetches. |
| 47 | Two boards cached independently | One board's refresh does not disturb the other. |
| 48 | Item field completeness | `id`, `name`, and both `text` and `value` survive the round trip. |

### 6.4 Live smoke — `test_live_smoke.py` (`@pytest.mark.live`)

Deselected by default; needs a real token. Run manually.

| # | Case | Expectation |
|---|------|-------------|
| 49 | `ME` | 200; returns the account id. Confirms auth end to end. |
| 50 | `LIST_BOARDS` | Returns the account's boards. |
| 51 | Items from the existing starter board | **Validates the real envelope shape** against our authored fixtures. |
| 52 | Complexity reported | `complexity.before` present; spend logged. |

### 6.5 Not tested here, deliberately

Normalization, canonical field names and data-quality measurement are F04's — F02 returns
monday.com's shape. Board seeding is F03's. Live verification against the *real* Deals and
Work Orders boards is impossible until F03 creates them (§7).

## 7. The honest risk: fixtures before boards

F02's tests are only as good as the fixtures, and the fixtures cannot yet be recorded from
the boards we care about — the live probe on 2026-08-31 found only two starter boards.

The plan:

1. **Author fixtures from documented response shapes** and build against them.
2. **Validate the envelope live against the starter board** (case 51). It is not our data,
   but it is the real API: `items_page`/`cursor` structure, `column_values` shape, error
   envelope and header behaviour are all exercised. This catches shape errors *now* rather
   than in F03.
3. **Re-record from the real boards in F03** and re-run the whole suite unchanged. If
   fixtures were wrong, F03 fails loudly at the point where it can be fixed cheaply.

Stated plainly: **"F02 COMPLETE" will mean "correct against the documented API and the
real envelope", not "verified against the real Deals and Work Orders boards."** That
second claim belongs to F03, and F02's doc will not make it.

## 8. Acceptance criteria

1. `uv run pytest` green, offline, no API key, no network.
2. All 48 offline cases (1-48) implemented and passing.
3. Coverage ≥90% on `bi_agent/monday/`.
4. The read-only gate rejects every adversarial case in §6.1 **and** produces no false
   positive on a legitimate query containing the word `mutation`.
5. No GraphQL string reaches the transport except from the `queries.py` registry.
6. No opaque monday.com column ID appears anywhere outside `bi_agent/monday/`.
7. A full DEBUG-level request cycle contains no token.
8. Live smoke tests (49-52) pass against the real account, with output recorded in §9.
9. Authored fixtures reconciled against the live envelope; any correction noted in §9.
10. This doc updated with real command output, then `STATUS: COMPLETE`, then one commit.

## 9. Implementation results

### 9.1 What was built

| File | Lines | Notes |
|------|-------|-------|
| `bi_agent/monday/queries.py` | 295 | Registry of 5 documents + the read-only verifier. |
| `bi_agent/monday/client.py` | 454 | Transport, `classify_failure`, retry/backoff. |
| `bi_agent/monday/boards.py` | 391 | Resolution, pagination, TTL cache, `BoardSnapshot`. |
| `bi_agent/monday/__init__.py` | 34 | Public surface. |
| `scripts/record_fixtures.py` | 248 | Live recorder + structural comparison. |
| `tests/unit/test_read_only_gate.py` | 262 | Cases 1-12. |
| `tests/unit/test_queries.py` | 99 | Registry integrity. |
| `tests/integration/test_client.py` | 644 | Cases 13-29 + classifier edge cases. |
| `tests/integration/test_boards.py` | 648 | Cases 30-48 + malformed payloads. |
| `tests/live/test_live_smoke.py` | 163 | Cases 49-52. |
| `tests/live/conftest.py` | 35 | Overrides `isolated_env` so live tests see the real `.env`. |
| `tests/fixtures/*.json` | 21 files | Authored, then reconciled against the live envelope (§9.4). |
| `tests/conftest.py` | modified | Fixture loader, `FakeClock`, `RecordedSleep`, client factory. |
| `pyproject.toml` | modified | `httpx` 0.28.1; dev `respx` 0.23.1. |
| `tests/fixtures/live/*.json` | 4 files | Recorded live, committed as evidence for §9.4. |

`bi_agent/config.py`, `errors.py` and `logging_config.py` were **not touched** —
F01 stayed closed, as §4 required.

### 9.2 Offline suite

```text
$ uv run pytest --cov=bi_agent.monday --cov-report=term-missing
tests/integration/test_boards.py ...................................  [ 17%]
tests/integration/test_client.py .................................... [ 38%]
tests/unit/test_config.py ............................               [ 51%]
tests/unit/test_errors.py .......................                    [ 62%]
tests/unit/test_logging_redaction.py ........................        [ 74%]
tests/unit/test_queries.py ..............                            [ 81%]
tests/unit/test_read_only_gate.py ..................................  [100%]

Name                          Stmts   Miss Branch BrPart  Cover   Missing
-------------------------------------------------------------------------
bi_agent\monday\__init__.py       4      0      0      0   100%
bi_agent\monday\boards.py       159      0     32      0   100%
bi_agent\monday\client.py       152      0     54      4    98%   99->94, 129->134, 132->129, 134->122
bi_agent\monday\queries.py       81      0     32      0   100%
-------------------------------------------------------------------------
TOTAL                           396      0    118      4    99%
===================== 199 passed, 5 deselected in 20.16s ======================
```

**99% coverage, 0 missed statements**, against a target of 90%. The four partial
branches in `client.py` are loop back-edges in the error-message scanner, taken
only when a payload carries several errors *and* the scan runs to the end without
matching — reachable, but not behaviourally distinct from cases already covered.

The suite runs with no `MONDAY_API_KEY` and no network: `respx` intercepts the
transport, and `isolated_env` clears every managed variable and disables `.env`.

### 9.3 Live smoke tests

```text
$ uv run pytest -m live -v -s
tests/live/test_live_smoke.py::test_me_confirms_authentication
account id: 83990706, is_admin: True                                    PASSED
tests/live/test_live_smoke.py::test_list_boards_returns_the_accounts_boards
3 boards: Build Vibe app (5030991691), Subitems of Your first board
(2075326945), Your first board (2075326943)                             PASSED
tests/live/test_live_smoke.py::test_real_envelope_matches_the_authored_fixture_shape
envelope OK: board 'Your first board', 5 columns, 4 items, cursor=None  PASSED
tests/live/test_live_smoke.py::test_complexity_is_reported_and_logged
complexity: before=999993 query=81 after=999912                         PASSED
tests/live/test_live_smoke.py::test_board_reader_paginates_a_real_board
'Your first board': 4 items over 1 page(s); columns: Date, Name, Person,
Status, Subitems                                                        PASSED

===================== 5 passed, 197 deselected in 19.15s ======================
```

Account `83990706` matches plan §9.2. An 81-point query against a ~1,000,000-point
budget confirms §3.4's expectation: complexity is observability here, not a
binding constraint.

**A bug in the live tests, found by the live tests.** The first run passed while
proving almost nothing: it read `boards[0]`, which on this account is an *empty*
subitems board, so the `column_values` assertions iterated zero items. A test that
passes vacuously is worse than no test, because it reports confidence it has not
earned. Fixed with a `board_with_items` fixture that searches for a board with
rows and skips explicitly if none has any.

### 9.4 Fixture reconciliation — the authored fixtures were wrong

`scripts/record_fixtures.py --compare` recorded the live envelope and diffed its
*structure* against the authored fixtures. `me`, `list_boards` and `board_columns`
came back **identical**. The item envelope did not:

```text
  board_items_page1.json vs authored board_items_single_page.json:
    DIFFERENT structure
      live    : "column_values": [{ "id": "str", "text": "null",
                                    "type": "str", "value": "null" }]
      authored: "column_values": [{ "id": "str", "text": "str",
                                    "type": "str", "value": "str" }]
```

Three real findings, all of which would otherwise have surfaced in F04:

1. **`text` can be JSON `null`, not just `""`.** Both forms occur on the *same*
   board: an unset people column returns `""`, an unset subtasks column returns
   `null`. The authored fixtures only ever had `""`. F02 now passes both through
   untouched — coercing here would erase the distinction one layer before F04 can
   decide what it means, which is the "zero used as missing" trap from
   DATA_PROFILE.md arriving a feature early.
2. **A column can exist without appearing in any item.** `name` is a column
   *definition*, but an item's name lives on `item["name"]` and never in
   `column_values`. Column resolution must not assume the two sets match.
3. **`value` JSON carries more keys than documented** — `changed_at`, `icon`,
   `personsAndTeams`. Harmless to F02, which passes raw JSON through, but F04's
   date parser must read `["date"]` and ignore the rest.

Fixtures were regenerated with a null-`text` column, and two regression tests now
pin findings 1 and 2. This is §7's plan working as intended: shape errors found
now, cheaply, instead of in F03.

The recordings themselves are committed under `tests/fixtures/live/` as the
evidence behind this section. They are not read by the offline suite. **They do
carry account-identifying content** — the account id (`83990706`), the token
holder's display name, and the contents of the starter board — so re-record them
deliberately rather than incidentally once the real boards exist, and check what
is in them before this repo goes public per plan OQ-3.

The comparison itself was corrected mid-run — it originally diffed whole envelopes
and reported `me.json` as different purely because the recorder writes only the
`data` sub-tree. Since `execute()` returns `data` and no caller ever sees the
surrounding envelope, the comparison now runs on `data`, which is the contract
that actually exists.

### 9.5 Deltas from the plan

Four, each small and each deliberate:

| Planned | Built | Why |
|---------|-------|-----|
| `BOARD_COLUMNS` fetched separately | `BOARD_ITEMS_FIRST` also returns `columns`; `BOARD_COLUMNS` retained for `fetch_columns()` | One request returns identity, columns and page one, holding to the 2-4 request budget. Costs a few complexity points. |
| Gate blacklists write keywords | Gate **whitelists** `query` / `fragment` | A blacklist must enumerate every spelling of a write; a whitelist refuses everything it does not recognise. It also keeps the forbidden word out of `bi_agent/` entirely, which is what makes case 12's sweep assertable. |
| `require_columns(titles)` unowned | Method on `BoardSnapshot` | §3.8 caches the title→ID map with the snapshot, so the check belongs where the map lives. |
| Variables named `$boardId` | `$boardIds` | It is a `[ID!]` list; the singular name was misleading. |

### 9.6 Acceptance criteria

| # | Criterion | Status |
|---|-----------|--------|
| 1 | `uv run pytest` green, offline, no key, no network | ✅ 199 passed |
| 2 | All 48 offline cases implemented and passing | ✅ plus ~40 supporting cases |
| 3 | Coverage ≥90% on `bi_agent/monday/` | ✅ **99%**, 0 missed statements |
| 4 | Gate rejects every adversarial case, no false positives | ✅ cases 2-6 and 10 rejected; 7-9 accepted |
| 5 | No GraphQL string reaches the transport except from the registry | ✅ enforced by type, plus case 11 and the case 12 sweep |
| 6 | No opaque column ID outside `bi_agent/monday/` | ✅ `grep` finds one occurrence, inside a docstring explaining the concept |
| 7 | A full DEBUG request cycle contains no token | ✅ case 14; case 28 proves redaction on the error path |
| 8 | Live smoke tests pass, output recorded | ✅ §9.3 |
| 9 | Fixtures reconciled against the live envelope | ✅ §9.4 — three corrections made |
| 10 | Doc updated with real output, marked COMPLETE, one commit | ✅ this section |

## 10. Known limitations

Stated plainly, in rough order of how much they matter.

1. **Not verified against the Deals or Work Orders boards.** They do not exist.
   The *envelope* is verified against a real board; the *schema* is verified
   against no board we care about. F03 re-records and re-runs this suite
   unchanged, and that is where any remaining fixture error will surface. This is
   §7's stated position, unchanged by implementation.
2. **The read-only gate is lexical, not a parser.** It tracks brace and paren
   depth over comment- and string-stripped text. It is defence in depth behind the
   registry, which is the real guarantee — no code path sends a caller-supplied
   string — and it is adversarially tested. A sufficiently exotic document could
   still confuse it; if the document set ever becomes dynamic, replace it with
   `graphql-core` as §3.3 anticipated.
3. **The token is still an admin token.** FR-5 is a guarantee about our code, not
   about the credential. Plan §9.2's open question — an OAuth `boards:read` app —
   remains open, and is worth deciding before F03 starts writing.
4. **Auth and rate-limit payload shapes are partly inferred.** The
   200-with-errors auth shape and the complexity message format come from
   documentation; the live account produced neither. The classifier keys on both
   status and message content precisely so a wrong guess degrades to a
   `MondayQueryError` rather than a crash — but the mapping stays a hypothesis
   until a real 429 arrives.
5. **`Retry-After` as an HTTP-date is ignored.** Legal per the RFC, unused by
   monday.com. We fall back to exponential backoff rather than risk mis-parsing a
   date into a decade-long sleep. Tested.
6. **The cache is per-process and in-memory.** Streamlit Community Cloud may run
   more than one process, in which case each keeps its own copy and the TTL is
   per-process. Acceptable at this scale; noted because the observable symptom —
   two users seeing different `fetched_at` values — looks like a bug.
7. **`fetch_items` holds a whole board in memory.** Correct at ~520 rows, per plan
   §3.4. Plan §10 records the ~10k-row boundary where this stops being true.
8. **No async.** F09 is Streamlit, which is synchronous, so two boards is two
   sequential requests. If board count grows, `httpx.AsyncClient` is a drop-in at
   this layer.
