# F01 — Configuration, Errors, and Project Skeleton

STATUS: **COMPLETE** — implemented and verified 2026-08-31. 75 tests, 100% statement/branch coverage on all three modules.

Parent plan: [00_IMPLEMENTATION_PLAN.md](../00_IMPLEMENTATION_PLAN.md)

---

## 1. Objective

Establish the foundation every later feature imports: environment-driven settings that
validate at startup, a typed exception hierarchy whose members each map to a defined
user-facing degradation, structured logging that cannot leak a secret, and a pytest
harness that runs fully offline.

This feature deliberately ships **no business logic**. Its value is that every subsequent
feature gets to assume "configuration is valid, errors are typed, logs are safe" instead
of re-deriving those guarantees. Getting secret handling right *before* a real token is in
play is the whole point — the token is already in `.env`, so the redaction guarantee must
exist before any code logs an HTTP request.

## 2. Requirement mapping

| Req | Statement | How F01 satisfies it |
|-----|-----------|----------------------|
| **FR-2** | Handle authentication and connection management | The auth half: the token is loaded, validated for presence and plausible shape, and exposed as a `SecretStr` that cannot be stringified into a log or traceback. F02 consumes it; F01 guarantees it exists and is well-formed. |
| **FR-16** | Graceful handling of API failures | Defines the exception hierarchy and the `user_message` contract that F02/F06 degrade against. Without typed errors, "graceful" reduces to a bare `except Exception`. |
| **NFR-3** | Reproducible, offline tests | `pytest` configured with markers separating unit from live tests; `conftest.py` guarantees no test reads the developer's real `.env`. |
| **NFR-4** | Secret handling | `SecretStr` throughout, `.env.example` committed with no values, a logging filter that redacts the live token value from every record, and a test that proves a leaked token does not reach the log stream. |
| **NFR-7** | Observability | Structured, level-configurable logging set up once, with a request-correlation field ready for F02. |
| **NFR-8** | Maintainability | No literal endpoint, timeout, model name, or board ID anywhere but `config.py`. |

## 3. Technical design

### 3.1 `bi_agent/config.py`

A single `Settings` class built on `pydantic-settings`, loaded once through a cached
`get_settings()`.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `monday_api_key` | `SecretStr` | — **required** | Name matches the existing `.env`. |
| `monday_api_url` | `str` | `https://api.monday.com/v2` | |
| `monday_api_version` | `str` | `2024-10` | Sent as the `API-Version` header; pinned so a server-side default change cannot silently alter responses. |
| `monday_deals_board_id` | `int \| None` | `None` | Populated by F03 after seeding. |
| `monday_work_orders_board_id` | `int \| None` | `None` | Same. |
| `anthropic_api_key` | `SecretStr \| None` | `None` | Optional until F06, so F01–F05 run without it. |
| `model` | `str` | `claude-sonnet-5` | |
| `cache_ttl_seconds` | `int` | `300` | §3.4 of the plan. |
| `http_timeout_seconds` | `float` | `30.0` | |
| `max_retries` | `int` | `3` | |
| `log_level` | `str` | `INFO` | |

Design decisions:

- **`SecretStr`, not `str`.** pydantic renders it as `**********` in `repr`, model dumps,
  and validation-error output. Validation errors echo offending values by default; a
  plain `str` token would land in a traceback the first time an unrelated field failed.
- **Optional Anthropic key.** Making it required would block F02–F05 behind a credential
  they do not use. It is validated at the point of use in F06 instead.
- **Board IDs optional and nullable.** They do not exist until F03 creates the boards.
  F02's board-resolution path must therefore also support lookup by name, which is the
  behaviour we want anyway (§2.4, column-ID indirection).
- **`get_settings()` is cached** (`functools.lru_cache`) so config is read once, and the
  cache is clearable so tests can inject different environments.
- **Validation failures raise `ConfigError`**, not pydantic's `ValidationError`, carrying
  a message naming the missing variable and how to obtain it. A stack trace is not an
  acceptable answer to "you forgot the token".

### 3.2 `bi_agent/errors.py`

```text
BIAgentError                      (base; carries .user_message)
├── ConfigError
├── MondayError
│   ├── MondayAuthError           401 / invalid token
│   ├── MondayRateLimitError      429 / complexity exceeded  (.retry_after)
│   ├── MondayUnavailableError    5xx, timeout, network
│   ├── MondayQueryError          GraphQL errors[] in a 200 response
│   ├── SchemaMismatchError       expected column absent     (.missing)
│   └── ReadOnlyViolationError    a mutation was attempted
├── DataError
│   └── NormalizationError        (.field, .raw_value)
├── QuerySpecError                model sent an invalid spec (.hint, for the model)
└── LLMError
```

The load-bearing detail is `user_message`. Each exception knows how it should be described
to a founder — separate from the developer-facing `str(exc)`. This is what makes FR-16
testable: "graceful degradation" becomes an assertion on a specific string, not a vibe.

`ReadOnlyViolationError` is defined here but raised in F02. It exists at the foundation
because FR-5 is a hard constraint, and the exception it raises should not be an
afterthought bolted on beside the code that violates it.

`QuerySpecError` carries `.hint` — a correction addressed to the *model*, not the user
(plan §4.3). F06 feeds it back as a tool error for retry.

### 3.3 `bi_agent/logging_config.py`

`configure_logging(level)` sets up a single root handler with a consistent format
including a `request_id` field (unused until F02, present from the start so the format
never changes).

The important component is `SecretRedactionFilter`: it holds the live secret values from
`Settings` and scrubs any occurrence of them from `record.msg` and `record.args` before
emission, substituting `***REDACTED***`.

Why a filter and not "just be careful": F02 will log request headers and error bodies on
failure paths, and monday.com echoes request context in some error payloads. The one time
a token leaks will be in an exception path nobody rehearsed. A filter is unconditional.
It is defence in depth behind `SecretStr`, not a replacement for it — `SecretStr` prevents
accidental interpolation; the filter catches deliberate-but-careless interpolation of
`.get_secret_value()`.

Cost, stated honestly: a substring scan per log record. Irrelevant at this volume.

### 3.4 Packaging and test harness

- **Flat `bi_agent/` package at the repo root**, per the layout decision now recorded in
  plan §3.6 — Streamlit Community Cloud does not install the project, so a `src/` layout
  would break only on the deployment target.
- `pyproject.toml` gains `pydantic`, `pydantic-settings`, `pytest`, `pytest-cov`, and a
  `[tool.pytest.ini_options]` block: `testpaths`, strict markers, and a `live` marker
  deselected by default so `uv run pytest` never touches the network.
- `tests/conftest.py` sets `_env_file=None` for settings constructed in tests and clears
  `get_settings`' cache between tests, so **no test can read the developer's real `.env`**
  — otherwise the suite passes locally and fails in CI, or worse, exercises a live token
  by accident.
- `main.py`'s stub is replaced with a small entrypoint that prints resolved, redacted
  configuration — a genuinely useful `uv run python main.py` setup check.

## 4. Files to create / modify

| File | Status | Responsibility |
|------|--------|----------------|
| `bi_agent/__init__.py` | create | Package marker, `__version__`. |
| `bi_agent/config.py` | create | `Settings`, `get_settings()`, `ConfigError` translation. |
| `bi_agent/errors.py` | create | Exception hierarchy with `user_message`. |
| `bi_agent/logging_config.py` | create | `configure_logging()`, `SecretRedactionFilter`. |
| `.env.example` | create | Documents every variable, values blank. Committed. |
| `main.py` | modify | Replace stub with a redacted config check. |
| `pyproject.toml` | modify | Deps + pytest config. |
| `tests/conftest.py` | create | Env isolation, cache clearing, shared fixtures. |
| `tests/unit/test_config.py` | create | Settings behaviour. |
| `tests/unit/test_errors.py` | create | Hierarchy and `user_message` contract. |
| `tests/unit/test_logging_redaction.py` | create | Proof that secrets do not reach logs. |

Not touched: `.env` (user-owned), the workbooks, `CLAUDE.md`, `PROBLEM-STATEMENT.md`.

## 5. Implementation plan

1. Restructure packaging: create `bi_agent/`, add deps, add pytest config.
2. Write `tests/` **first** — the three test modules below, all failing.
3. Implement `errors.py` (no dependencies).
4. Implement `config.py`.
5. Implement `logging_config.py`.
6. Rewrite `main.py` as the setup check.
7. Write `.env.example`.
8. Run the suite; fix; re-run until green.
9. Run `main.py` against the real `.env` to confirm the live token loads and prints
   redacted.
10. Record actual output in §8 of this doc, mark COMPLETE, commit.

## 6. Test plan

Written before implementation, per EXECUTION.md rule 4.

### `test_config.py`
| # | Case | Expectation |
|---|------|-------------|
| 1 | All required env vars present | Settings loads; every field takes its expected value. |
| 2 | `MONDAY_API_KEY` absent | `ConfigError` naming the variable; **not** `ValidationError`. |
| 3 | `MONDAY_API_KEY` empty / whitespace-only | `ConfigError` — an empty token is a missing token. |
| 4 | Defaults | URL, API version, TTL, timeout, retries, model match §3.1 with no env set. |
| 5 | Env overrides defaults | e.g. `CACHE_TTL_SECONDS=60` wins. |
| 6 | `repr(settings)` and `model_dump()` | Token value appears in neither; `**********` appears. |
| 7 | `ANTHROPIC_API_KEY` absent | Loads fine, field is `None` — F01–F05 are not blocked. |
| 8 | Board IDs absent | Both `None`, no error. |
| 9 | Board ID non-numeric | `ConfigError`, not a crash at first use. |
| 10 | `get_settings()` caching | Two calls return the same object; cache clear yields a new one. |
| 11 | Key with surrounding whitespace (`MONDAY_API_KEY =…`, as in the real `.env`) | Parses correctly. Regression guard: verified against python-dotenv on 2026-08-31, pinned by test so a loader change cannot silently break the real file. |

### `test_errors.py`
| # | Case | Expectation |
|---|------|-------------|
| 12 | Every concrete error subclasses `BIAgentError` | Enforced by iterating the hierarchy, so a future error cannot be added outside it. |
| 13 | Every concrete error has a non-empty `user_message` | Contract holds for all members. |
| 14 | `user_message` never contains a secret-shaped substring | Guards against templating a token into user text. |
| 15 | `MondayRateLimitError.retry_after` | Preserved when supplied, `None` otherwise. |
| 16 | `SchemaMismatchError.missing` | Lists the absent column(s). |
| 17 | `QuerySpecError.hint` | Present and model-directed. |
| 18 | Catching `MondayError` catches all monday subclasses | The degradation strategy in plan §4.3 depends on this grouping. |

### `test_logging_redaction.py`
| # | Case | Expectation |
|---|------|-------------|
| 19 | Token interpolated into a log message | Emitted record contains `***REDACTED***`, not the token. |
| 20 | Token passed as a lazy `%s` arg | Redacted too — the common real-world case. |
| 21 | Token inside a dict/JSON body being logged | Redacted. |
| 22 | Token appearing in an exception traceback that is logged | Redacted. |
| 23 | Unrelated messages | Passed through unmodified — the filter must not corrupt normal logs. |
| 24 | Filter with no secrets configured | No-op, no crash. |
| 25 | `configure_logging` called twice | Handlers not duplicated (Streamlit reruns the script on every interaction — without this, log output multiplies on every user turn). |

### Not tested here, deliberately
Network behaviour, retries, and board access are F02's; asserting them now would require
stubbing code that does not exist. Test 11 is the only one grounded in a live observation,
and it is pinned precisely because it was observed rather than assumed.

## 7. Acceptance criteria

1. `uv run pytest` is green, offline, with no `.env` present and no network.
2. All 25 cases above implemented and passing.
3. Coverage on `bi_agent/config.py`, `errors.py`, `logging_config.py` ≥ 95%.
4. `uv run python main.py` against the real `.env` reports config loaded with the token
   shown redacted, and the token string appears nowhere in stdout or logs.
5. `grep` for the live token across all tracked files returns nothing.
6. No literal URL, timeout, model name, or board ID outside `config.py`.
7. `.env.example` lists every variable with empty values and is committed.
8. This doc updated with real command output, then `STATUS: COMPLETE`, then one commit.

## 8. Implementation results

Implemented 2026-08-31. All 25 planned cases are covered; they expanded to **75 tests**,
because several plan rows (blank token, secret-shaped output, scrub paths) are only
meaningfully tested as a family rather than as a single assertion.

### 8.1 Test suite

```text
$ uv run pytest --cov=bi_agent --cov-report=term-missing

tests\unit\test_config.py ............................                   [ 37%]
tests\unit\test_errors.py .......................                        [ 68%]
tests\unit\test_logging_redaction.py ........................            [100%]

=============================== tests coverage ================================
______________ coverage: platform win32, python 3.12.11-final-0 _______________

Name                         Stmts   Miss Branch BrPart  Cover   Missing
------------------------------------------------------------------------
bi_agent\__init__.py             1      0      0      0   100%
bi_agent\config.py             129      0     40      0   100%
bi_agent\errors.py              52      0      2      0   100%
bi_agent\logging_config.py      84      0     36      0   100%
------------------------------------------------------------------------
TOTAL                          266      0     78      0   100%
============================= 75 passed in 0.31s ==============================
```

Statement **and** branch coverage are 100% on all three modules, against a 95% target.

### 8.2 The suite runs with no `.env` present

`.env` was moved aside and the suite re-run, to show the isolation in `conftest.py` is
real rather than incidental:

```text
$ mv .env .env.tmpmoved && uv run pytest -q
75 passed in 0.15s

$ uv run python main.py
Configuration check FAILED

Configuration is invalid. MONDAY_API_KEY is not set. Create a token in monday.com
under your avatar - Developers - My Access Tokens, then add it to .env (see .env.example).
exit=1
```

### 8.3 `main.py` against the real `.env`

```text
$ uv run python main.py
bi_agent 0.1.0 - configuration check passed

Resolved settings (secrets redacted):
  monday_api_key               **********
  monday_api_url               https://api.monday.com/v2
  monday_api_version           2024-10
  monday_deals_board_id        (not set)
  monday_work_orders_board_id  (not set)
  anthropic_api_key            (not set)
  model                        claude-sonnet-5
  cache_ttl_seconds            300
  http_timeout_seconds         30.0
  max_retries                  3
  log_level                    INFO

Readiness:
  monday.com token          loaded
  board IDs                 not set yet (created and recorded by F03)
  Anthropic key             not set yet (first needed by F06)

# stderr
2026-08-31T13:07:04 INFO     [setup] bi_agent.setup: configuration loaded
```

The real 228-character token loads and prints redacted. Automated leak check over stdout,
stderr and every tracked file:

```text
token length                  : 228
full token in main.py output  : False
token prefix (12) in output   : False
mask present in output        : True
tracked files scanned         : 25
tracked files containing token: []
```

### 8.4 Mutation check — the tests are load-bearing

A suite that is green on its first run deserves suspicion, so each of the three guarantees
was deliberately broken and the suite re-run:

| Mutation | Result |
|----------|--------|
| `SecretRedactionFilter.filter` returns before scrubbing | **12 failed**, 63 passed |
| `load_settings` re-raises `ValidationError` instead of `ConfigError` | **14 failed**, 61 passed |
| `configure_logging` appends handlers instead of replacing | **1 failed**, 74 passed |
| all mutations reverted | 75 passed |

### 8.5 Acceptance criteria

| # | Criterion | Evidence |
|---|-----------|----------|
| 1 | Suite green, offline, no `.env` | §8.2 — 75 passed with `.env` moved aside. Nothing in the suite imports an HTTP client. |
| 2 | All 25 cases implemented | 75 tests; each test section is labelled with the case numbers it covers. |
| 3 | Coverage ≥95% on the three modules | §8.1 — 100% statement and branch on each. |
| 4 | `main.py` loads the real token and prints it redacted | §8.3 — exit 0, mask shown, token absent from stdout and stderr. |
| 5 | Live token in no tracked file | §8.3 — 25 tracked files scanned, zero hits. |
| 6 | No literal URL, timeout, model or board ID outside `config.py` | `grep -rnE` for `api.monday.com`, `claude-*`, `https?://`, 6+ digit numbers and `timeout=` over `bi_agent/` and `main.py`, excluding `config.py` → no matches. |
| 7 | `.env.example` committed, every variable, no values | Created; `git check-ignore` confirms `.env` is ignored and `.env.example` is not. |
| 8 | Doc updated with real output, marked COMPLETE, one commit | This section. |

### 8.6 Decisions taken during implementation

- **`model` accepts two env names.** Plan §9.2 documents `BI_AGENT_MODEL`; the pydantic
  field-name default would be `MODEL`. Rather than pick one and silently contradict the
  plan, the field uses `AliasChoices("BI_AGENT_MODEL", "MODEL")`, and `BI_AGENT_MODEL` is
  the name reported in error messages.
- **Blank means absent, for optional variables.** `ANTHROPIC_API_KEY=` and
  `MONDAY_DEALS_BOARD_ID=` left empty in `.env` resolve to `None`, not to an empty string
  and not to a validation error. `.env.example` ships those keys blank, so the alternative
  would make a freshly copied example file fail to load.
- **Blank is *not* absent for the required token.** An empty `MONDAY_API_KEY` raises
  immediately rather than reaching F02 and surfacing later as a confusing 401.
- **A second filter, `RequestIdFilter`.** `request_id` is in the log format from day one
  (§3.3), which means any record that does not set it would raise `KeyError` inside the
  logging machinery. The filter defaults it to `-`.
- **Two branches deleted rather than tested.** An emptiness guard in `secret_values()` and
  a bare-string-alias branch in the error-location map were both unreachable. Writing
  tests to reach them would have meant testing code that cannot run, so they were removed
  instead — which is what took coverage from 94% to 100%.
- **`log_level` accepts only strings.** An `int` level raises `ConfigError`. Environment
  variables are always strings, so this costs nothing and keeps the accepted set explicit.

## 9. Known limitations

- **The redaction filter only scrubs values it was told about.** `configure_logging`
  receives `settings.secret_values()`, so a credential introduced later — an OAuth token
  in F03, say — is not covered until it is registered via `add_secret`. F02 must pass any
  new credential in.
- **Redaction is exact-substring.** A token logged split across two arguments, or
  URL-encoded, or truncated, would not match. `SecretStr` remains the primary defence; the
  filter is the second layer, not the only one.
- **`MIN_REDACTABLE_LENGTH = 8` is a deliberate hole.** A secret shorter than 8 characters
  is ignored, because redacting a short string would corrupt ordinary log lines. No
  credential in this project is that short.
- **The 20-character token floor is a sanity check, not validation.** It catches a
  truncated paste. Only a live API call — F02 — can establish that a token actually works.
- **Filters mutate the record.** A scrubbed record carries the redacted text onward, so a
  second handler cannot see the original. That is the intent, but it does mean the filter
  must stay attached to the handler rather than to a logger, or records reaching other
  handlers would bypass it.
- **`conftest.py` patches `Settings.model_config["env_file"]`.** That is a global
  mutation, reverted by `monkeypatch`. It is the mechanism keeping the suite off the
  developer's real `.env`; anything constructing `Settings` outside pydantic-settings'
  normal path would sidestep it.
- **No `live` tests exist yet.** The marker and its deselection are configured and
  verified (`uv run pytest -m live` collects nothing), but F02 is the first feature with
  anything to run under it.
