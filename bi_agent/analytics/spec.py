"""`MetricResult` and `QuerySpec`: plan section 3.2 option C, made concrete.

The model never performs arithmetic and never sees a raw row it then has to add up. It
picks a tool and arguments (F06); `QuerySpec` validates those arguments against the known
schema *before* anything runs, and every result is a `MetricResult` carrying its own
coverage — never a bare number. An invalid spec becomes a `QuerySpecError` with a `hint`
the model can act on (F01), not a user-facing failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, field_validator

from bi_agent.data.schema import DEALS_FIELDS, WORK_ORDERS_FIELDS, field_by_canonical
from bi_agent.errors import QuerySpecError

__all__ = ["BOARD_FIELDS", "Filter", "MetricResult", "QuerySpec", "validate_spec"]

FilterOp = Literal["eq", "ne", "in", "gt", "gte", "lt", "lte", "is_null", "not_null"]
MetricKind = Literal["count", "sum", "avg", "min", "max"]
BoardName = Literal["deals", "work_orders"]

BOARD_FIELDS = {"deals": DEALS_FIELDS, "work_orders": WORK_ORDERS_FIELDS}

#: Fields every board carries that are not part of the per-board schema table but are
#: always safe to filter/group on: the derived `is_junk` flag and the item id.
_STRUCTURAL_FIELDS = {"is_junk", "item_id"}

_AGGREGATING_METRICS: frozenset[MetricKind] = frozenset({"sum", "avg", "min", "max"})


@dataclass(frozen=True)
class MetricResult:
    """Never a bare number. `caveats` is generated from measured coverage, not phrased
    by the model (CLAUDE.md's "surface caveats" rule, made structural)."""

    value: float | int | None
    unit: str
    n_used: int
    n_total: int
    excluded: dict[str, int] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    #: For money metrics: which figure this is (OQ-5) — "deal_value" (pipeline),
    #: "billed" (revenue) or "collected" (cash). `None` for non-money metrics.
    basis: str | None = None


class Filter(BaseModel):
    field: str
    op: FilterOp
    value: Any = None

    @field_validator("value")
    @classmethod
    def _in_requires_a_list(cls, value: Any, info: Any) -> Any:
        if info.data.get("op") == "in" and not isinstance(value, list):
            raise ValueError("op 'in' requires a list value")
        return value


class QuerySpec(BaseModel):
    board: BoardName
    filters: list[Filter] = []
    group_by: list[str] = []
    metric: MetricKind
    field: str | None = None


def _known_fields(board: BoardName) -> set[str]:
    return {spec.canonical for spec in BOARD_FIELDS[board]} | _STRUCTURAL_FIELDS


def validate_spec(spec: QuerySpec) -> None:
    """Structural validation against the schema — no data needed.

    Raises :class:`QuerySpecError` naming every problem, not just the first, and always
    with a `hint` a model can act on without a second failed attempt.
    """
    known = _known_fields(spec.board)
    problems: list[str] = []

    for filt in spec.filters:
        if filt.field not in known:
            problems.append(f"unknown filter field {filt.field!r}")

    for name in spec.group_by:
        if name not in known:
            problems.append(f"unknown group_by field {name!r}")

    if spec.metric == "count":
        if spec.field is not None and spec.field not in known:
            problems.append(f"unknown field {spec.field!r}")
    else:
        if spec.field is None:
            problems.append(f"metric {spec.metric!r} requires 'field'")
        elif spec.field not in known:
            problems.append(f"unknown field {spec.field!r}")
        elif spec.metric in _AGGREGATING_METRICS:
            fields_by_name = field_by_canonical(BOARD_FIELDS[spec.board])
            spec_field = fields_by_name.get(spec.field)
            # Checked in this order deliberately: a field explicitly flagged
            # `summable=False` gets the specific, actionable reason (mixed units)
            # even though it is also non-numeric — "not numeric" alone would be
            # true but would hide the actual cause from CLAUDE.md's mixed-unit
            # quantity columns.
            if spec_field is not None and not spec_field.summable:
                problems.append(
                    f"{spec.field!r} is not summable — mixed/inconsistent units in the "
                    "source data (see CLAUDE.md); ask a question that does not require "
                    "aggregating it, or request the raw values instead"
                )
            elif spec_field is None or spec_field.field_type != "number":
                problems.append(
                    f"{spec.field!r} is not numeric; metric {spec.metric!r} does not apply"
                )

    if problems:
        hint = (
            "Fix: " + "; ".join(problems) + ". Call describe_data to list the valid "
            f"fields for board {spec.board!r}, then retry."
        )
        raise QuerySpecError("; ".join(problems), hint=hint)


def validate_categorical_values(spec: QuerySpec, frame: pd.DataFrame) -> None:
    """A second pass, against real data: an `eq`/`in` filter whose value matches nothing
    observed fails fast, with the valid values in the hint, rather than silently
    returning zero rows and letting the model believe that is a real answer."""
    for filt in spec.filters:
        if filt.op not in ("eq", "in") or filt.field not in frame.columns:
            continue
        if pd.api.types.is_numeric_dtype(frame[filt.field]):
            continue
        observed = set(frame[filt.field].dropna().unique())
        wanted = filt.value if isinstance(filt.value, list) else [filt.value]
        unknown = [value for value in wanted if value not in observed]
        if unknown:
            sample = ", ".join(sorted(str(v) for v in observed)[:15])
            raise QuerySpecError(
                f"{filt.field!r} has no value(s) {unknown!r} in the data",
                hint=(
                    f"Valid observed values for {filt.field!r} include: {sample}. "
                    "Call describe_data for the full list, then retry."
                ),
            )
