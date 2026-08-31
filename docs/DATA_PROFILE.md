# Data Profile — verified against the source workbooks

STATUS: Complete (evidence for design decisions in `00_IMPLEMENTATION_PLAN.md`)

Everything below was measured with `openpyxl` against the two workbooks in the repo root,
not assumed. Row numbers are Excel numbering (1-based, header included).

## Board A — Deals

`Deal funnel Data.xlsx` → sheet `Deal tracker`, 348 x 12, header on **row 1**.
347 data rows → **344 real deals** after dropping 1 fully-empty row and 2 embedded header rows.

| # | Column | Non-null | Notes |
|---|--------|----------|-------|
| 0 | Deal Name | 344/347 | **not unique** — 154 distinct names over 342 rows |
| 1 | Owner code | 329/347 | OWNER_001..007 |
| 2 | Client Code | 344/347 | `COMPANY089` style |
| 3 | Deal Status | 345/347 | Won 165, Dead 127, Open 49, On Hold 2, **`Deal Status` 2 (junk)** |
| 4 | Close Date (A) | 28/347 | 26 real dates, 2024-12-31 → 2026-01-15; 318 empty strings |
| 5 | Closure Probability | 88/347 | High/Medium/Low + 2 junk |
| 6 | Masked Deal value | 165/347 | 165 numeric, **181 empty strings**; sum 2,305,518,041; range 51,440 → 751,473,450; no zeros |
| 7 | Tentative Close Date | 272/347 | 270 real, 2024-09-30 → 2026-04-01 |
| 8 | Deal Stage | 346/347 | 17 distinct incl. junk; see below |
| 9 | Product deal | 176/347 | Pure Service 151, then a long tail of `Dock + DMO + Spectra` combos |
| 10 | Sector/service | 338/347 | 12 distinct incl. junk |
| 11 | Created Date | 345/347 | 343 real, **2024-08-09 → 2026-01-09** |

**Embedded header rows** at Excel rows **52** (`Nezuko`) and **181** (`Bugs Bunny`): a real deal
name in column A, literal header text in every other column. Detect on
`Deal Status == 'Deal Status'` — filtering on `Deal Name` misses them. They inject
`Sector/service`, `Deal Stage`, `Closure Probability`, `Product deal` and `Close Date (A)`
as spurious category values.

**Stage vocabulary** (mostly `A. `-prefixed, but not all):
`A. Lead Generated` 74, `H. Work Order Received` 46, `L. Project Lost` 42,
`E. Proposal/Commercials Sent` 28, `G. Project Won` 27, `M. Projects On Hold` 20,
`N. Not relevant at the moment` 19, **`Project Completed` 19 (unprefixed)**,
`O. Not Relevant at all` 18, `B. Sales Qualified Leads` 14, `F. Negotiations` 13,
`C. Demo Done` 9, `J. Invoice sent` 6, `D. Feasibility` 4, `I. POC` 3,
`K. Amount Accrued` 2, junk 2.

**Status contradicts stage.** Of 165 `Won` deals, **70 sit at `A. Lead Generated`** and
2 at `F. Negotiations`. Only 26 are at `G. Project Won`. `Deal Status` alone is not
trustworthy; 45 `Won` are at `H. Work Order Received` and 19 at `Project Completed`,
which are self-consistent.

**Sectors** (12): Renewables 111, Mining 106, Railways 40, Others 28, Powerline 26,
Construction 9, DSP 7, Tender 5, Manufacturing 2, Security and Surveillance 1, Aviation 1,
junk 2.

## Board B — Work Orders

`Work_Order_Tracker Data.xlsx` → sheet `work order tracker`, 179 x 38,
**header on row 2** (row 1 blank). 177 data rows → **176 real work orders** after dropping
1 fully-empty row. No embedded header rows here.

`Serial #` (`SDPLDEAL-075`) is **unique across all 176** — the only true primary key in
either dataset.

Fully empty columns (**0/177**): `Expected Billing Month`, `Actual Collection Month`,
`Collection status`, `Collection Date`. Any collection-timing question is unanswerable.

Sparse columns: `AR Priority account` 10/177, `Last executed month of recurring project`
15/177, `Quantity billed (till date)` 23/177, `Billing Status` 28/177,
`Quantity by Ops` 43/177, `Data Delivery Date` 58/177, `Actual Billing Month` 70/177,
`Collected Amount` 78/177, `Last invoice date` 87/177, `latest invoice no.` 88/177,
`WO Status (billed)` 102/177, `Invoice Status` 112/177, `Billed Value (Excl GST)` 113/177.

**Zero used as missing** — counted on numeric cells only:
`Billed Value (Incl of GST)` 63 zeros, `Amount to be billed (Excl/Incl)` 70 each,
`Amount Receivable` 77, `Amount (Excl of GST)` 6, `Amount (Incl of GST)` 7.

**One hard Excel error**: `Amount in Rupees (Excl of GST) (Masked)` contains a literal
`#VALUE!` string in one row. Must be coerced to null, counted, and reported — never to 0.

**Empty string as null** in numeric columns: Billed Value (Excl) 63, Collected Amount 98,
Quantity by Ops 133, Quantity billed 153, Balance in quantity 19.

**Date ranges**: `Date of PO/LOI` 2022-09-29 → 2026-01-12 (175 values);
`Probable Start Date` 2024-07-30 → 2026-01-19; **`Probable End Date` 2025-04-10 →
2028-03-31** — the 2028 tail sits far beyond every other range and will distort any
"this quarter" framing; `Last invoice date` 2025-03-31 → 2026-01-14;
`Data Delivery Date` 2025-01-23 → 2026-01-09.

**Categoricals**: Sector 6 (Mining 100, Renewables 51, Railways 13, Powerline 6, Others 4,
Construction 2). Execution Status 7 (Completed 117, Ongoing 25, `Executed until current
month` 12, Not Started 11, `Pause / struck` 4, `Partial Completed` 2, `Details pending from
Client` 1). Invoice Status 6, including one-off `Billed- Visit 7`, `Billed- Visit 3`,
`Stuck`. Billing Status 5 with a casing bug: **`BIlled`**. Nature of Work 4.
`Type of Work` is a **comma-joined multi-select** — 36 distinct strings over ~12 atoms
(`Topography Survey: RGB` alone or combined with `Hydrology`, `Others`, etc.).

**`Quantities as per PO` is free text with mixed units** (164 values): 91 bare numbers with
no unit at all, then HA 29, ACRES 7, DAYS 5, `NA` 4, MONTHS 3, KM 3, TOWERS 3, RKM 2, and
singletons ACR, ACERS, MW, AU, LOCATION, MINES, PILLARS, ROOFTOPS, SITES, SUBSCRIPTIONS,
UNITS, `L S`, `Rate based on MW slabs`, `Quarter till Dec`, `Quarter till December`,
`NA verbal confirmation for km`. Three spellings of acre. **Not summable across rows.**

## Cross-board reality

- **No shared key.** `Serial #` exists only on Work Orders; Deals have no ID column.
- Joining on **Deal Name is many-to-many**: 58 distinct work-order names against 154
  distinct deal names where `Sakura` appears 27 times, `Alphonse` 19, `Ben Tennyson` 16.
  A name join silently multiplies revenue.
- **6 work-order names have no deal record**: `Dolphin`, `Whale`, `Octopus`, `Turtle`,
  `Golden fish`, `GG go`.
- **Client codes are unrelated** across boards: `COMPANY089` vs `WOCOMPANY_002`.
- **Owner codes nearly align** but not fully: deals use OWNER_001–007, work orders
  OWNER_001–006 **plus OWNER_008**. Volume distribution differs sharply
  (deals: OWNER_003 174; work orders: OWNER_001 87).

Consequence: comparisons across boards are safe only on **shared dimensions**
(sector, owner code, time period) computed independently per board and presented
side-by-side. Row-level joins are not.

## Currency and fiscal signals

Amounts are in Rupees, GST-inclusive/exclusive pairs are modelled explicitly, and invoice
numbers read `SDPL/FY25-26/916`. This is an **Indian fiscal year (April–March)** business.
`FY25-26` = 2025-04-01 → 2026-03-31, so Q1 = Apr–Jun, Q2 = Jul–Sep, Q3 = Oct–Dec,
Q4 = Jan–Mar.
