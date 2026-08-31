"""`column_values` -> typed value -> canonical DataFrame.

Parses from `text` uniformly across column types (F04/F05 plan section 3.3) — the same
field F03's own live verification trusted for exact numeric round-tripping
(`scripts/seed_monday.py::_sum_numbers_column`). One parser path per type; nothing branches
on `value`'s JSON shape.

Three outcomes per cell, matching `scripts/seeding/schema.py`'s `EncodeResult` on the way
in: empty (`None`, unremarkable), unrepresentable (`None`, counted — the read-side mirror
of "never coerce to zero"), or a real value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import pandas as pd

from bi_agent.data.schema import (
    DEALS_FIELDS,
    DEALS_ITEM_NAME_FIELD,
    DEALS_JUNK_MARKER_FIELD,
    DEALS_JUNK_MARKER_VALUE,
    WON_CONSISTENT_STAGES,
    WORK_ORDERS_FIELDS,
    WORK_ORDERS_ITEM_NAME_FIELD,
    FieldSpec,
)
from bi_agent.monday.boards import BoardSnapshot

__all__ = [
    "NormalizedBoard",
    "normalize_deals",
    "normalize_work_orders",
    "parse_cell",
]

#: Matches the letter prefix on a `Deal Stage` value (`"A. Lead Generated"` -> `"A"`).
#: The unprefixed `Project Completed` and every junk row correctly yield no match.
_STAGE_LETTER_RE = re.compile(r"^([A-Z])\.\s")

#: The one casing bug CLAUDE.md names by example. Fixed for grouping; the raw value
#: survives in `billing_status_raw` so it stays visible to a data-quality question.
_BILLING_STATUS_CASING_FIXES = {"BIlled": "Billed"}


def parse_cell(field_type: str, text: str | None) -> tuple[Any, bool]:
    """`(value, unrepresentable)`. `value` is `None` for both empty and unrepresentable."""
    if text is None or text == "":
        return None, False

    if field_type == "date":
        try:
            return date.fromisoformat(text), False
        except ValueError:
            return None, True

    if field_type == "number":
        try:
            return float(text), False
        except ValueError:
            return None, True

    if field_type == "list":
        return [part.strip() for part in text.split(",") if part.strip()], False

    return text, False


@dataclass(frozen=True)
class NormalizedBoard:
    board: str
    frame: pd.DataFrame
    #: canonical field -> count of non-empty text that failed to parse.
    unrepresentable: dict[str, int] = field(default_factory=dict)
    n_junk_rows: int = 0
    #: e.g. `{"BIlled -> Billed": 3}`.
    casing_fixes: dict[str, int] = field(default_factory=dict)


def _normalize_board(
    snapshot: BoardSnapshot,
    *,
    board_name: str,
    fields: tuple[FieldSpec, ...],
    item_name_field: str,
    junk_marker_field: str | None,
    junk_marker_value: str | None,
) -> NormalizedBoard:
    unrepresentable = {spec.canonical: 0 for spec in fields}
    rows: list[dict[str, Any]] = []

    for item in snapshot.items:
        row: dict[str, Any] = {
            item_name_field: item.get("name") or None,
            "item_id": str(item.get("id")),
        }
        text_by_id = {cv.get("id"): cv.get("text") for cv in item.get("column_values") or []}
        for spec in fields:
            column_id = snapshot.column_id(spec.header)
            text = text_by_id.get(column_id) if column_id else None
            value, bad = parse_cell(spec.field_type, text)
            if bad:
                unrepresentable[spec.canonical] += 1
            row[spec.canonical] = value
        rows.append(row)

    frame = pd.DataFrame(rows)

    if junk_marker_field and junk_marker_field in frame.columns:
        is_junk = frame[junk_marker_field] == junk_marker_value
    else:
        is_junk = pd.Series(False, index=frame.index)
    frame["is_junk"] = is_junk

    return NormalizedBoard(
        board=board_name,
        frame=frame,
        unrepresentable=unrepresentable,
        n_junk_rows=int(is_junk.sum()),
    )


def _extract_stage_letter(stage: Any) -> str | None:
    if not isinstance(stage, str):
        return None
    match = _STAGE_LETTER_RE.match(stage)
    return match.group(1) if match else None


def normalize_deals(snapshot: BoardSnapshot) -> NormalizedBoard:
    normalized = _normalize_board(
        snapshot,
        board_name="deals",
        fields=DEALS_FIELDS,
        item_name_field=DEALS_ITEM_NAME_FIELD,
        junk_marker_field=DEALS_JUNK_MARKER_FIELD,
        junk_marker_value=DEALS_JUNK_MARKER_VALUE,
    )
    frame = normalized.frame
    frame["stage_letter"] = frame["stage"].apply(_extract_stage_letter)
    frame["has_value"] = frame["deal_value"].notna()
    frame["stage_status_consistent"] = [
        status != "Won" or stage in WON_CONSISTENT_STAGES
        for status, stage in zip(frame["status"], frame["stage"], strict=True)
    ]
    return replace(normalized, frame=frame)


def _ratio(numerator: Any, denominator: Any) -> float | None:
    if denominator is None or pd.isna(denominator) or denominator <= 0:
        return None
    if numerator is None or pd.isna(numerator):
        return None
    return float(numerator) / float(denominator)


def normalize_work_orders(snapshot: BoardSnapshot) -> NormalizedBoard:
    normalized = _normalize_board(
        snapshot,
        board_name="work_orders",
        fields=WORK_ORDERS_FIELDS,
        item_name_field=WORK_ORDERS_ITEM_NAME_FIELD,
        junk_marker_field=None,
        junk_marker_value=None,
    )
    frame = normalized.frame

    frame["billing_status_raw"] = frame["billing_status"]
    casing_fix_count = 0
    for raw, fixed in _BILLING_STATUS_CASING_FIXES.items():
        matched = frame["billing_status"] == raw
        casing_fix_count += int(matched.sum())
        frame.loc[matched, "billing_status"] = fixed

    frame["is_billed"] = frame["billed_incl_gst"].fillna(0) > 0
    frame["billing_pct"] = [
        _ratio(billed, amount)
        for billed, amount in zip(frame["billed_incl_gst"], frame["amount_incl_gst"], strict=True)
    ]
    frame["collection_pct"] = [
        _ratio(collected, billed)
        for collected, billed in zip(
            frame["collected_incl_gst"], frame["billed_incl_gst"], strict=True
        )
    ]

    casing_fixes = {"BIlled -> Billed": casing_fix_count} if casing_fix_count else {}
    return replace(normalized, frame=frame, casing_fixes=casing_fixes)
