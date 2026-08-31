"""Side-by-side board comparison on a shared dimension — never a row-level join.

OQ-7 / CLAUDE.md: Deals and Work Orders share no reliable key. `Serial #` is the only true
primary key anywhere in the data and Deals have no equivalent; deal names are not unique
(346 deal rows carry only 155 distinct names, `Sakura` alone is 27 of them), so joining on
name would silently multiply revenue. `compare_boards` therefore never joins rows: it
restricts the model to dimensions genuinely tracked, under the same canonical name, on
both boards (`sector`, `owner_code` — see `schema.py`) and refuses anything else with a
stated reason, so a request that is really asking for a row join fails loudly instead of
producing a number nobody can trust.
"""

from __future__ import annotations

from dataclasses import dataclass

from bi_agent.analytics.metrics import run_query
from bi_agent.analytics.spec import MetricResult, QuerySpec
from bi_agent.data.repository import BoardData
from bi_agent.errors import QuerySpecError

__all__ = ["CROSSBOARD_DIMENSIONS", "CrossBoardComparison", "compare_boards"]

#: Fields carried under the same canonical name on both boards (schema.py) — the only
#: axes safe for a side-by-side comparison. Deliberately excludes `deal_name`: grouping
#: on it would look like a join and inherit exactly the many-to-many problem CLAUDE.md
#: warns about, without ever calling it a join.
CROSSBOARD_DIMENSIONS = frozenset({"sector", "owner_code"})


@dataclass(frozen=True)
class CrossBoardComparison:
    dimension: str
    deals: dict[str, MetricResult]
    work_orders: dict[str, MetricResult]
    #: Dimension values observed on one board only — a real asymmetry between the boards'
    #: vocabularies (CLAUDE.md: deals carry 12 sectors, work orders 6), not a computation
    #: gap, so it is surfaced rather than left for the model to notice or miss.
    deals_only_keys: list[str]
    work_orders_only_keys: list[str]
    caveats: list[str]


def compare_boards(
    deals: BoardData,
    work_orders: BoardData,
    *,
    dimension: str,
    deals_metric: str = "sum",
    deals_field: str | None = "deal_value",
    wo_metric: str = "sum",
    wo_field: str | None = "billed_incl_gst",
) -> CrossBoardComparison:
    """Group each board by `dimension` independently and hand back both results side by
    side. Raises `QuerySpecError` — the model's correctable-error path (F01/F06) — for
    any dimension outside `CROSSBOARD_DIMENSIONS`, which is where a row-join request
    (`dimension="deal_name"`, `"serial_no"`) is refused, explicitly and by name."""
    if dimension not in CROSSBOARD_DIMENSIONS:
        raise QuerySpecError(
            f"cannot compare boards on {dimension!r}",
            hint=(
                "Deals and Work Orders share no reliable row-level key — deal names are "
                "not unique (e.g. 'Sakura' is 27 separate deal rows) and there is no "
                "shared identifier, so row-by-row joins are refused (CLAUDE.md). Compare "
                f"on a shared dimension instead: {sorted(CROSSBOARD_DIMENSIONS)}."
            ),
        )

    deals_grouped = run_query(
        QuerySpec(board="deals", group_by=[dimension], metric=deals_metric, field=deals_field),
        deals,
    )
    wo_grouped = run_query(
        QuerySpec(board="work_orders", group_by=[dimension], metric=wo_metric, field=wo_field),
        work_orders,
    )

    deals_by_key = {str(key): value for key, value in deals_grouped.items()}
    wo_by_key = {str(key): value for key, value in wo_grouped.items()}

    deals_only = sorted(set(deals_by_key) - set(wo_by_key))
    wo_only = sorted(set(wo_by_key) - set(deals_by_key))

    caveats = [
        f"Side-by-side comparison on {dimension!r}, not a row-level join: Deals and Work "
        "Orders share no reliable key, so these figures are not linked record-to-record.",
    ]
    if deals_only:
        caveats.append(
            f"{dimension!r} value(s) seen only in Deals, absent from Work Orders: "
            f"{', '.join(deals_only)}."
        )
    if wo_only:
        caveats.append(
            f"{dimension!r} value(s) seen only in Work Orders, absent from Deals: "
            f"{', '.join(wo_only)}."
        )

    return CrossBoardComparison(
        dimension=dimension,
        deals=deals_by_key,
        work_orders=wo_by_key,
        deals_only_keys=deals_only,
        work_orders_only_keys=wo_only,
        caveats=caveats,
    )
