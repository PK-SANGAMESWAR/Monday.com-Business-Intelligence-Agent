"""Board schema as data: workbook header -> monday.com column type -> encoder.

Column typing follows CLAUDE.md / F03 section 3.4, decision D-2: **`date` for
dates, `numbers` for money, `text` for everything else** — including every
categorical (`Deal Stage`, `Invoice Status`, `Billing Status`, ...). None of
those are closed vocabularies (junk values, casing bugs like `BIlled`,
one-offs like `Billed- Visit 7`), so a `status`/`dropdown` column would either
silently coerce or reject them. `text` is the type that cannot lie about the
source.

Two encoding rules are load-bearing and have direct tests:

* **zero is written as `"0"`, never omitted.** 63 work orders have a real,
  recorded zero in `Billed Value (Incl GST)`; omitting the column would turn
  a recorded zero into an indistinguishable empty cell (CLAUDE.md: "zero as
  missing" is exactly the bug this guards against).
* **empty stays empty, unrepresentable is reported, never coerced to 0.**
  `#VALUE!` in a numbers column and free text in a date column are both
  "unrepresentable", not "empty" — the caller is expected to omit the column
  from `column_values` *and* record the raw value for the seeding report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

__all__ = [
    "ColumnSpec",
    "ColumnType",
    "DEALS_COLUMNS",
    "DEALS_ITEM_NAME_HEADER",
    "EncodeResult",
    "SOURCE_ROW_COLUMN_TYPE",
    "SOURCE_ROW_HEADER",
    "WORK_ORDERS_COLUMNS",
    "WORK_ORDERS_ITEM_NAME_HEADER",
    "encode_source_row",
    "encode_value",
]

ColumnType = Literal["date", "numbers", "text"]

#: Our column-type vocabulary maps 1:1 onto monday.com's `ColumnType` enum
#: values for the three types this board ever uses.
MONDAY_COLUMN_TYPE: dict[ColumnType, str] = {
    "date": "date",
    "numbers": "numbers",
    "text": "text",
}


@dataclass(frozen=True)
class ColumnSpec:
    """One workbook column: its exact header text and its monday.com type."""

    header: str
    column_type: ColumnType


# --- Deals: 12 headers, `Deal Name` becomes the item name -------------------

DEALS_ITEM_NAME_HEADER = "Deal Name"

DEALS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("Owner code", "text"),
    ColumnSpec("Client Code", "text"),
    ColumnSpec("Deal Status", "text"),
    ColumnSpec("Close Date (A)", "date"),
    ColumnSpec("Closure Probability", "text"),
    ColumnSpec("Masked Deal value", "numbers"),
    ColumnSpec("Tentative Close Date", "date"),
    ColumnSpec("Deal Stage", "text"),
    ColumnSpec("Product deal", "text"),
    ColumnSpec("Sector/service", "text"),
    ColumnSpec("Created Date", "date"),
)

# --- Work Orders: 38 headers, `Serial #` becomes the item name --------------
# `Serial #` is the only true primary key in either dataset (unique across all
# 176 rows) — see CLAUDE.md — so it gets the item's visible identity, and
# `Deal name masked` (not unique) becomes an ordinary text column.

WORK_ORDERS_ITEM_NAME_HEADER = "Serial #"

WORK_ORDERS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("Deal name masked", "text"),
    ColumnSpec("Customer Name Code", "text"),
    ColumnSpec("Nature of Work", "text"),
    ColumnSpec("Last executed month of recurring project", "text"),
    ColumnSpec("Execution Status", "text"),
    ColumnSpec("Data Delivery Date", "date"),
    ColumnSpec("Date of PO/LOI", "date"),
    ColumnSpec("Document Type", "text"),
    ColumnSpec("Probable Start Date", "date"),
    ColumnSpec("Probable End Date", "date"),
    ColumnSpec("BD/KAM Personnel code", "text"),
    ColumnSpec("Sector", "text"),
    ColumnSpec("Type of Work", "text"),
    ColumnSpec(
        "Is any Skylark software platform part of the client deliverables "
        "in this deal?",
        "text",
    ),
    ColumnSpec("Last invoice date", "date"),
    ColumnSpec("latest invoice no.", "text"),
    ColumnSpec("Amount in Rupees (Excl of GST) (Masked)", "numbers"),
    ColumnSpec("Amount in Rupees (Incl of GST) (Masked)", "numbers"),
    ColumnSpec("Billed Value in Rupees (Excl of GST.) (Masked)", "numbers"),
    ColumnSpec("Billed Value in Rupees (Incl of GST.) (Masked)", "numbers"),
    ColumnSpec("Collected Amount in Rupees (Incl of GST.) (Masked)", "numbers"),
    ColumnSpec("Amount to be billed in Rs. (Exl. of GST) (Masked)", "numbers"),
    ColumnSpec("Amount to be billed in Rs. (Incl. of GST) (Masked)", "numbers"),
    ColumnSpec("Amount Receivable (Masked)", "numbers"),
    ColumnSpec("AR Priority account", "text"),
    ColumnSpec("Quantity by Ops", "text"),
    ColumnSpec("Quantities as per PO", "text"),
    ColumnSpec("Quantity billed (till date)", "text"),
    ColumnSpec("Balance in quantity", "text"),
    ColumnSpec("Invoice Status", "text"),
    ColumnSpec("Expected Billing Month", "text"),
    ColumnSpec("Actual Billing Month", "text"),
    ColumnSpec("Actual Collection Month", "text"),
    ColumnSpec("WO Status (billed)", "text"),
    ColumnSpec("Collection status", "text"),
    ColumnSpec("Collection Date", "date"),
    ColumnSpec("Billing Status", "text"),
)

# --- provenance ---------------------------------------------------------------
# Not in either source workbook. Justified in F03 section 3.6: without a
# per-row key, 346 deals over 154 distinct names cannot be deduplicated, so
# seeding would not be idempotent or resumable. `DEAL-0052` / `WO-0113`.

SOURCE_ROW_HEADER = "Source Row"
SOURCE_ROW_COLUMN_TYPE: ColumnType = "text"


def encode_source_row(prefix: str, source_row: int) -> str:
    """`DEAL-0052` / `WO-0113` — zero-padded so it sorts as text too."""
    return f"{prefix}-{source_row:04d}"


# --- encoding -------------------------------------------------------------


@dataclass(frozen=True)
class EncodeResult:
    """The outcome of encoding one cell for one column.

    Three outcomes, deliberately distinct: ``"empty"`` (the workbook cell was
    genuinely blank — omit the column, nothing to report), ``"value"`` (the
    JSON-ready payload for `column_values`), ``"unrepresentable"`` (the cell
    held something this type cannot express — omit the column *and* report
    the raw value; never coerce it to zero or drop it silently).
    """

    kind: Literal["empty", "value", "unrepresentable"]
    value: Any = None
    raw: Any = None


def _format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _encode_date(raw: Any) -> EncodeResult:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return EncodeResult("empty")
    if isinstance(raw, datetime):
        return EncodeResult("value", {"date": raw.date().isoformat()})
    if isinstance(raw, date):
        return EncodeResult("value", {"date": raw.isoformat()})
    # A date-typed cell holding text (CLAUDE.md: `#VALUE!` and friends) is
    # unrepresentable, never a crash and never coerced.
    return EncodeResult("unrepresentable", raw=raw)


def _encode_number(raw: Any) -> EncodeResult:
    if raw is None:
        return EncodeResult("empty")
    if isinstance(raw, bool):
        return EncodeResult("unrepresentable", raw=raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped == "":
            return EncodeResult("empty")
        try:
            number: int | float = (
                int(stripped)
                if stripped.lstrip("-").isdigit()
                else float(stripped)
            )
        except ValueError:
            # `#VALUE!` lands here: reported, omitted, never 0.
            return EncodeResult("unrepresentable", raw=raw)
        return EncodeResult("value", _format_number(number))
    if isinstance(raw, (int, float)):
        return EncodeResult("value", _format_number(raw))
    return EncodeResult("unrepresentable", raw=raw)


def _encode_text(raw: Any) -> EncodeResult:
    if raw is None:
        return EncodeResult("empty")
    if isinstance(raw, str):
        if raw.strip() == "":
            return EncodeResult("empty")
        return EncodeResult("value", raw)
    if isinstance(raw, datetime):
        return EncodeResult("value", raw.isoformat())
    if isinstance(raw, date):
        return EncodeResult("value", raw.isoformat())
    return EncodeResult("value", str(raw))


def encode_value(column_type: ColumnType, raw: Any) -> EncodeResult:
    """Encode one workbook cell for `column_values`, per `column_type`."""
    if column_type == "date":
        return _encode_date(raw)
    if column_type == "numbers":
        return _encode_number(raw)
    return _encode_text(raw)
