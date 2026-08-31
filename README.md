# monday.com Business Intelligence Agent

An AI agent that answers founder-level business questions — *"How's our pipeline looking
for the energy sector this quarter?"* — by querying two live monday.com boards (Work
Orders and Deals), cleaning genuinely messy real-world data, and answering with a number
plus the caveats that number deserves. Full requirements: [PROBLEM-STATEMENT.md](PROBLEM-STATEMENT.md).
Design rationale: [docs/00_IMPLEMENTATION_PLAN.md](docs/00_IMPLEMENTATION_PLAN.md) and
[docs/DECISION_LOG.md](docs/DECISION_LOG.md).

## Architecture

```text
                    +----------------------------+
   browser  ------->|  Streamlit chat UI (app.py)|
   (hosted link)    +-------------+--------------+
                                  | user turn
                    +-------------v--------------+
                    |  Agent core (bi_agent/agent)|
                    |  Anthropic tool-use loop    |
                    +-------------+--------------+
                       structured tool calls
                    +-------------v--------------+
                    |  Analytics (bi_agent/analytics)
                    |  pandas metrics, validated  |
                    |  query spec, coverage data  |
                    +-------------+--------------+
                    +-------------v--------------+
                    |  Normalization & quality    |
                    |  (bi_agent/data)             |
                    +-------------+--------------+
                    +-------------v--------------+
                    |  monday.com client           |
                    |  (bi_agent/monday)           |
                    |  GraphQL, retry, TTL cache,  |
                    |  read-only gate              |
                    +-------------+--------------+
                                  | HTTPS
                          monday.com API v2
```

The model never does arithmetic: it picks a tool, tested Python in `bi_agent/analytics/`
returns `{value, n_used, n_total, excluded, caveats}`, and the model's job is to phrase
that as an answer. Full write-up: [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md).

## Repository layout

```text
bi_agent/         agent package (config, monday client, data, analytics, agent core)
app.py            Streamlit entrypoint (repo root — required by Streamlit Cloud)
scripts/          seed_monday.py: one-off writer, xlsx -> monday boards (not agent code)
tests/            unit / integration / live
docs/             implementation plan, decision log, architecture, testing, feature docs
*.xlsx            sample data — schema reference and board-seed source only, never a
                  runtime data source (the agent always queries monday.com live)
```

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo>
cd Monday.com-Business-Intelligence-Agent
uv sync
cp .env.example .env
```

Fill in `.env`:

| Variable | Required for | Where to get it |
|----------|---------------|------------------|
| `MONDAY_API_KEY` | seeding, and every board read | monday.com → your avatar → Developers → My Access Tokens |
| `ANTHROPIC_API_KEY` | the agent core (chat) | console.anthropic.com → API Keys |
| `MONDAY_DEALS_BOARD_ID` | reading the Deals board | printed by `scripts/seed_monday.py` after seeding, or the board's URL |
| `MONDAY_WORK_ORDERS_BOARD_ID` | reading the Work Orders board | same |

Everything else (`MONDAY_API_URL`, `BI_AGENT_MODEL`, `CACHE_TTL_SECONDS`, …) has a sane
default — see `.env.example` and `bi_agent/config.py`.

### Testing without an Anthropic key (local Ollama)

No `ANTHROPIC_API_KEY` yet? Set `LLM_PROVIDER=ollama` in `.env` to run the same agent
against a local model instead — nothing else in the codebase changes:

```bash
ollama serve                 # if not already running
ollama pull qwen2.5:7b       # any tool-calling-capable model works (llama3.1, qwen2.5, …)
```

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
```

Once an Anthropic key is issued, set `LLM_PROVIDER=anthropic` (or remove the line) and fill
in `ANTHROPIC_API_KEY` — that one variable is the entire switch back. See
`bi_agent/agent/ollama_client.py` and the Decision Log for how the two backends stay
interchangeable.

## monday.com board setup

The two monday.com boards do not exist by default — the `.xlsx` files are the only copy
of the sample data, and the agent needs them seeded onto real boards before it can answer
anything. This is scripted, not manual, so it is repeatable and testable:

```bash
uv run python scripts/seed_monday.py --dry-run          # preview: what would be created
uv run python scripts/seed_monday.py                    # create both boards
uv run python scripts/seed_monday.py --only work-orders  # or just one
```

The script reads both workbooks, infers a monday.com column type for each source column
(status, numbers, date, text, …), creates a board per workbook if one does not already
exist, and creates one item per row with a rate limit (`--items-per-minute`, default keeps
well under monday's API limits). It prints the created board IDs — put those in
`MONDAY_DEALS_BOARD_ID` / `MONDAY_WORK_ORDERS_BOARD_ID`. It is the **only** part of this
codebase that writes to monday.com; the agent package itself is read-only by construction
(`bi_agent/monday/client.py` rejects any GraphQL document containing a `mutation`
operation before it is sent). See `docs/SEEDING_REPORT.md` for the last real seeding run's
output and `docs/features/03_BOARD_SEEDING.md` for the full design.

## Running

```bash
uv run streamlit run app.py
```

Opens a chat UI at `localhost:8501`. The sidebar shows board row counts, always-empty
fields, and stage/status conflict counts (the same data-quality facts the agent's caveats
are built from), plus a manual "refresh board data" button to bypass the cache TTL.

## Testing

```bash
uv run pytest                                             # offline, no API key needed
uv run pytest --cov=bi_agent --cov-report=term-missing    # with coverage
uv run pytest -m live                                     # needs a real MONDAY_API_KEY
```

Full breakdown of what each tier covers: [docs/testing/TESTING.md](docs/testing/TESTING.md).
Last recorded run: [docs/FINAL_VALIDATION.md](docs/FINAL_VALIDATION.md).

## Deploying (Streamlit Community Cloud)

1. Push this repository to a GitHub account you control (the current `origin` remote is
   already a public GitHub repo — the sample data is masked, so this is safe to share).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with that GitHub
   account, and click "New app".
3. Point it at this repo, branch `main`, main file `app.py`.
4. In the app's **Settings → Secrets**, add:
   ```toml
   MONDAY_API_KEY = "..."
   ANTHROPIC_API_KEY = "..."
   MONDAY_DEALS_BOARD_ID = "..."
   MONDAY_WORK_ORDERS_BOARD_ID = "..."
   ```
   (Same keys as `.env`; Streamlit's secrets are injected as environment variables, which
   is exactly what `bi_agent/config.py` reads from — no code changes needed between local
   and hosted.)
5. Deploy. Streamlit Cloud installs from `requirements.txt` (kept in sync with
   `pyproject.toml`/`uv.lock` — regenerate after any dependency change with
   `uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt`).

This step needs the repo owner's own GitHub/Streamlit identity and API keys, so it is not
something this codebase can complete on its own — everything up to "click Deploy" is done
and verified.

## Known limitations

See [docs/DECISION_LOG.md](docs/DECISION_LOG.md) "What we'd do differently" and
[CLAUDE.md](CLAUDE.md) for the full list of verified data-quality issues (contradictory
statuses, all-empty columns, mixed-unit free text, an unreliable cross-board key). The
short version: cross-board answers are always approximate and say so; four columns
(collection timing) are unanswerable and the agent says that rather than inventing a
proxy; a personal monday.com token cannot be scoped read-only at the credential level, so
read-only is enforced in this codebase's client instead.
