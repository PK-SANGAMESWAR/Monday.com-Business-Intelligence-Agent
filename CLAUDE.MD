# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

An AI agent that answers founder-level business intelligence questions ("How's our
pipeline looking for the energy sector this quarter?") by querying two monday.com
boards — **Work Orders** (project execution) and **Deals** (sales pipeline).

Full requirements live in [PROBLEM-STATEMENT.md](PROBLEM-STATEMENT.md). Read it before
making design decisions.

## Current state

Greenfield. `main.py` is a stub; the only real assets are the spec and the two sample
workbooks. Python 3.12, managed with **uv** (`uv.lock`, `pyproject.toml`) — use
`uv add <pkg>` and `uv run <cmd>`, not bare `pip`/`python`. Reading the workbooks needs
`openpyxl`; add it with `uv add` rather than assuming it is present.

## Hard constraints

- **Never hardcode the sample data.** The agent must query monday.com dynamically at
  runtime. The files are for seeding the boards and local schema reference only.
- **monday.com access is read-only.** No mutations to boards, items, or columns.
- Integration via monday.com MCP server or REST/GraphQL API — either is acceptable.
- Secrets (API tokens) go in `.env`, which is gitignored. Never commit a token.

## The data

Two `.xlsx` workbooks, one sheet each — the only copy of the sample data.

`Deal funnel Data.xlsx` → sheet `Deal tracker`, **346 x 12**, header row 1. Created dates
span 2024-08 to 2026-01; deal values 51K-751M (masked), sum ~2.31B.

`Work_Order_Tracker Data.xlsx` → sheet `work order tracker`, **176 x 38**, **header on
row 2** (row 1 is blank — `pd.read_excel(..., header=1)`). Covers billing and collection:
amounts excl./incl. GST, billed, collected, receivable, quantities, invoices, status.

### Joining the two boards

There is **no reliable key.** Verified:

- `Serial #` (`SDPLDEAL-075`) is unique across all 176 work orders — the only true
  primary key anywhere in the data. Deals have no equivalent ID.
- Deal names are **not unique**: 346 deal rows carry only 155 distinct names (`Sakura`
  appears 27 times, `Alphonse` 19). Name + Client Code still leaves 32 duplicates.
  Joining on name is many-to-many and will silently multiply revenue.
- Of 58 distinct work order deal names, 52 match a deal; 6 (`Dolphin`, `Whale`,
  `Octopus`, `Turtle`, `Golden fish`, `GG go`) have no deal record.
- Owner codes nearly align: deals use OWNER_001-007, work orders OWNER_001-006 plus
  **OWNER_008**. Client codes are unrelated across boards (`COMPANY089` vs
  `WOCOMPANY_002`).

Treat cross-board answers as approximate and say so. Prefer answering from one board.

## Data is genuinely messy — verified issues

- **Embedded header rows** at deal rows 52 and 181 (Excel numbering): a real deal name in
  column A, header text in every other cell — so filtering on `Deal Name == 'Deal Name'`
  misses them. Detect on `Deal Status == 'Deal Status'`. They leak junk into every value
  list (`Sector/service` as a sector, `Closure Probability` as a probability).
- **Status contradicts stage.** 165 deals are `Won`, but 70 of those sit at stage
  `A. Lead Generated`. Never trust `Deal Status` alone — cross-check the stage and tell
  the user when the two disagree.
- **Four columns are 100% empty**: Expected Billing Month, Actual Collection Month,
  Collection status, Collection Date. Any collection-timing question is unanswerable —
  say so rather than inventing a proxy.
- **Zero used as missing**: 63 of 176 work orders have Billed Value (incl. GST) = 0.
  Distinguish "billed nothing" from "not recorded" before averaging.
- **Heavy nulls**: Close Date 92%, Closure Probability 75%, deal value 52%, Product deal
  49%, Collected Amount 56%, AR Priority 94%.
- **`Quantities as per PO` is free text with mixed units** — HA, ACRES/ACERS/ACR (three
  spellings), KM, RKM, MW, IMAGES, PILLARS, ROOFTOPS, MONTHS, plus prose like
  `NA verbal confirmation for km`. Not summable; parse unit-by-unit or refuse.
- **Inconsistent categoricals**: deal stages mostly prefixed (`A. Lead Generated`) but
  some not (`Project Completed`). Deals carry 12 sectors, work orders 6 — `Aviation`,
  `Tender`, `DSP`, `Manufacturing`, `Security and Surveillance` are deals-only.
- Probable End Date runs to **2028-03-31**, well past every other date range — check
  outliers before framing anything as "this quarter".

Rules: normalize dates and labels before aggregating, drop junk rows explicitly rather
than crashing, and **surface caveats** — "this total covers the 165 of 346 deals that
have a value recorded." Silent dropping of records is a bug.

## Behaviour expectations

- Conversational. Ask a clarifying question when a query is genuinely ambiguous
  (which quarter? pipeline value or count?); don't interrogate the user otherwise.
- Answer with **insight plus number**, not a bare figure — trend, comparison, or what
  the number implies.
- Degrade gracefully on API failure or partial data; say what failed and answer with
  what is available.

## Deliverables to keep in mind

A hosted, link-accessible prototype; a Decision Log (2 pages max: assumptions,
trade-offs, what you'd do differently, and how "leadership updates" was interpreted);
and a README with architecture and monday.com board setup instructions.
