# Testing

STATUS: Complete. Numbers below are from the actual run recorded in
`docs/FINAL_VALIDATION.md` (2026-08-31) — regenerate this file's numbers if the suite
changes materially.

## Running the suite

```bash
uv sync
uv run pytest                                    # offline, no API key needed
uv run pytest --cov=bi_agent --cov-report=term-missing   # with coverage
```

No network access and no `MONDAY_API_KEY` / `ANTHROPIC_API_KEY` are required for the
default run — `pyproject.toml` sets `addopts = "-m 'not live'"`, so the one tier that
needs real credentials is deselected unless asked for explicitly:

```bash
uv run pytest -m live      # needs a real MONDAY_API_KEY; hits monday.com
```

## Test tiers

- **`tests/unit/`** — cleaners, parsers, the query-spec validator, calendar resolution,
  the read-only write gate, tool schemas, the agent loop with a stubbed Anthropic client.
  Table-driven against real messy values pulled from the workbooks (`#VALUE!`, empty
  string, the embedded junk-header rows, `BIlled`, `5360 HA`, `Project Completed` without
  a stage prefix).
- **`tests/integration/`** — the monday.com client against `respx`-mocked HTTP responses
  (cursor pagination across pages, 429 backoff, 5xx retry then stale-cache fallback, auth
  failure, malformed payload), the repository's fetch→normalize→cache path, the seeding
  script's mutation-building and dry-run mode, and the Streamlit app's construction order.
- **`tests/live/`** — deselected by default; runs the same client/repository code against
  the real, seeded monday.com boards, and is the only tier that proves DL-4 (the boards
  actually exist and round-trip real data) rather than a mock of it.

## Golden-value tests

Metric outputs are checked against figures computed independently from the source
workbooks with `openpyxl`/`pandas` outside the application code — e.g. deal-value sum
across exactly the deals that carry a value, 176 unique work-order serials, the 63
zero-billed work orders. These catch a normalization regression that a hand-picked test
row would miss.

## Coverage

From `docs/FINAL_VALIDATION.md`: **96% overall**, branch coverage on. `data/` and
`agent/` modules are at 96–100%; `analytics/metrics.py` is the one module below the 90%
target for its layer (80%), missing mostly seldom-hit metric-argument-error branches
(unknown field name, wrong metric for a categorical field) — noted as a known limitation
rather than backfilled with tests that would just restate the code.

## What is deliberately not tested

- Real Anthropic model responses — the agent loop is tested against a stubbed client, so
  wording quality is a manual/live concern, not a unit-test one.
- The actual Streamlit-rendered browser UI — `tests/integration/test_app.py` exercises
  `app.py`'s construction and session logic, not pixels; UI review is manual (README
  §Manual verification).
