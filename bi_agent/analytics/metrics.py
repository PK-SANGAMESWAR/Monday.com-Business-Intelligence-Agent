"""The query engine plus named, high-level metrics. Every path returns `MetricResult`.

`run_query` is plan section 3.2 option C: filter -> group_by -> aggregate, generic and
fully validated. The named functions below it are thin, tested wrappers that exist for
the questions CLAUDE.md and the problem statement name directly (revenue, pipeline,
sector, collections) — none of them hand-roll their own filtering.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import pandas as pd

from bi_agent.analytics.calendar import Period, most_recent_period_with_data, resolve_period
from bi_agent.analytics.spec import Filter, MetricResult, QuerySpec, validate_categorical_values, validate_spec
from bi_agent.data.repository import BoardData
from bi_agent.errors import QuerySpecError

__all__ = [
    "MONEY_FIELDS",
    "always_null_fields_report",
    "apply_filters",
    "collected_amount",
    "pipeline_value",
    "receivable",
    "revenue_billed",
    "run_query",
    "sector_breakdown",
    "stage_distribution",
]

#: Fields whose unit is Rupees, for `MetricResult.unit`.
MONEY_FIELDS = frozenset(
    {
        "deal_value",
        "amount_excl_gst",
        "amount_incl_gst",
        "billed_excl_gst",
        "billed_incl_gst",
        "collected_incl_gst",
        "to_bill_excl_gst",
        "to_bill_incl_gst",
        "receivable",
    }
)


def _unit_for(field_name: str | None) -> str:
    if field_name in MONEY_FIELDS:
        return "INR"
    if field_name and field_name.endswith("_pct"):
        return "ratio"
    return ""


def apply_filters(frame: pd.DataFrame, filters: list[Filter]) -> pd.DataFrame:
    mask = pd.Series(True, index=frame.index)
    for filt in filters:
        column = frame[filt.field]
        if filt.op == "eq":
            mask &= column == filt.value
        elif filt.op == "ne":
            mask &= column != filt.value
        elif filt.op == "in":
            mask &= column.isin(filt.value)
        elif filt.op == "gt":
            mask &= column > filt.value
        elif filt.op == "gte":
            mask &= column >= filt.value
        elif filt.op == "lt":
            mask &= column < filt.value
        elif filt.op == "lte":
            mask &= column <= filt.value
        elif filt.op == "is_null":
            mask &= column.isna()
        elif filt.op == "not_null":
            mask &= column.notna()
    return frame.loc[mask]


def _aggregate(frame: pd.DataFrame, spec: QuerySpec) -> MetricResult:
    n_total = len(frame)

    if spec.metric == "count":
        if spec.field is None:
            return MetricResult(value=n_total, unit="count", n_used=n_total, n_total=n_total)
        present = frame[spec.field].notna()
        n_used = int(present.sum())
        return MetricResult(value=n_used, unit="count", n_used=n_used, n_total=n_total)

    column = frame[spec.field] if n_total else pd.Series(dtype=float)
    present = column.notna()
    n_used = int(present.sum())
    excluded = {"value_missing": n_total - n_used} if n_total - n_used else {}
    unit = _unit_for(spec.field)

    if n_used == 0:
        return MetricResult(
            value=None,
            unit=unit,
            n_used=0,
            n_total=n_total,
            excluded=excluded,
            caveats=[f"No rows in scope have a recorded {spec.field}."],
        )

    values = column[present]
    value = {
        "sum": values.sum(),
        "avg": values.mean(),
        "min": values.min(),
        "max": values.max(),
    }[spec.metric]

    caveats = []
    if n_used < n_total:
        caveats.append(
            f"This {spec.metric} covers {n_used} of {n_total} rows that have a recorded "
            f"{spec.field}; the rest are excluded, not counted as zero."
        )

    return MetricResult(
        value=float(value), unit=unit, n_used=n_used, n_total=n_total, excluded=excluded, caveats=caveats
    )


def run_query(
    spec: QuerySpec, data: BoardData
) -> MetricResult | dict[Any, MetricResult]:
    """Filter -> group_by -> aggregate. Junk rows (deals) are always excluded first."""
    validate_spec(spec)

    frame = data.frame
    n_junk = int(frame["is_junk"].sum()) if "is_junk" in frame.columns else 0
    scoped = frame.loc[~frame["is_junk"]] if "is_junk" in frame.columns else frame

    validate_categorical_values(spec, scoped)
    scoped = apply_filters(scoped, spec.filters)

    if not spec.group_by:
        result = _aggregate(scoped, spec)
        if n_junk:
            result = replace(
                result,
                excluded={**result.excluded, "junk_row": n_junk},
                caveats=[
                    *result.caveats,
                    f"{n_junk} data-entry error row(s) were excluded from this figure "
                    "(CLAUDE.md).",
                ],
            )
        return result

    return {
        # pandas' groupby(list_of_one_column) yields 1-tuple keys; unwrap so a caller
        # sees "Won", not "('Won',)" - the raw tuple form leaked into every grouped tool
        # result the model saw, undermining FR-14's "insight, not raw output" for no
        # reason (multi-column group_by, unused so far, keeps its tuple key).
        (key[0] if len(spec.group_by) == 1 else key): _aggregate(group_frame, spec)
        for key, group_frame in scoped.groupby(spec.group_by, dropna=False)
    }


def _resolve_period_with_fallback(
    frame: pd.DataFrame, date_field: str, period_text: str, *, now: date
) -> tuple[Period, list[str]]:
    try:
        requested = resolve_period(period_text, now)
    except ValueError as exc:
        raise QuerySpecError(
            str(exc),
            hint=(
                f"{period_text!r} was not understood. Use 'this quarter', 'last quarter', "
                "'this fiscal year', 'last fiscal year', or 'FY25-26' / 'FY25-26 Q3'."
            ),
        ) from exc

    dates = pd.to_datetime(frame[date_field], errors="coerce")
    in_range = (dates.dt.date >= requested.start) & (dates.dt.date <= requested.end)
    if in_range.any():
        return requested, []

    fallback = most_recent_period_with_data(frame[date_field])
    if fallback is None:
        return requested, [
            f"No rows have a recorded {date_field}, so no period could be resolved at all."
        ]
    return fallback, [
        f"No rows fall in {requested.label}; showing {fallback.label}, the most recent "
        f"period with data, instead."
    ]


def pipeline_value(
    data: BoardData,
    *,
    sector: str | None = None,
    status: str | None = None,
    period: str | None = None,
    date_field: str = "tentative_close_date",
    now: date | None = None,
) -> MetricResult:
    """Sum of `deal_value` — pipeline, not revenue (OQ-5)."""
    filters = []
    if sector is not None:
        filters.append(Filter(field="sector", op="eq", value=sector))
    if status is not None:
        filters.append(Filter(field="status", op="eq", value=status))

    extra_caveats: list[str] = []
    if period is not None:
        resolved, extra_caveats = _resolve_period_with_fallback(
            data.frame, date_field, period, now=now or date.today()
        )
        filters.append(Filter(field=date_field, op="gte", value=resolved.start))
        filters.append(Filter(field=date_field, op="lte", value=resolved.end))

    spec = QuerySpec(board="deals", filters=filters, metric="sum", field="deal_value")
    result = run_query(spec, data)
    return replace(result, caveats=[*result.caveats, *extra_caveats], basis="deal_value")


def revenue_billed(
    data: BoardData,
    *,
    sector: str | None = None,
    period: str | None = None,
    date_field: str = "last_invoice_date",
    now: date | None = None,
) -> MetricResult:
    """Sum of `billed_incl_gst` — CLAUDE.md/OQ-5's default meaning of "revenue"."""
    filters = []
    if sector is not None:
        filters.append(Filter(field="sector", op="eq", value=sector))

    extra_caveats: list[str] = []
    if period is not None:
        resolved, extra_caveats = _resolve_period_with_fallback(
            data.frame, date_field, period, now=now or date.today()
        )
        filters.append(Filter(field=date_field, op="gte", value=resolved.start))
        filters.append(Filter(field=date_field, op="lte", value=resolved.end))

    spec = QuerySpec(board="work_orders", filters=filters, metric="sum", field="billed_incl_gst")
    result = run_query(spec, data)
    return replace(
        result,
        caveats=[
            "Basis: billed value (revenue), not deal value (pipeline) or collected cash.",
            *result.caveats,
            *extra_caveats,
        ],
        basis="billed",
    )


def collected_amount(data: BoardData) -> MetricResult:
    """Sum of `collected_incl_gst`. Never period-scoped: `Collection Date` is always-null
    (CLAUDE.md) so collection *timing* is unanswerable — the caveat says so explicitly
    rather than the function pretending a date filter would work."""
    spec = QuerySpec(board="work_orders", metric="sum", field="collected_incl_gst")
    result = run_query(spec, data)
    return replace(
        result,
        caveats=[
            *result.caveats,
            "Collection timing (which quarter money was collected in) cannot be answered: "
            "Collection Date is empty for every record.",
        ],
        basis="collected",
    )


def receivable(data: BoardData) -> MetricResult:
    spec = QuerySpec(board="work_orders", metric="sum", field="receivable")
    result = run_query(spec, data)
    return replace(result, basis="receivable")


def stage_distribution(data: BoardData) -> dict[Any, MetricResult]:
    spec = QuerySpec(board="deals", group_by=["stage"], metric="count")
    return run_query(spec, data)


def sector_breakdown(data: BoardData, *, board: str, metric: str, field: str | None = None) -> dict[Any, MetricResult]:
    spec = QuerySpec(board=board, group_by=["sector"], metric=metric, field=field)
    return run_query(spec, data)


def always_null_fields_report(data: BoardData) -> list[str]:
    return data.quality.always_null_fields()
