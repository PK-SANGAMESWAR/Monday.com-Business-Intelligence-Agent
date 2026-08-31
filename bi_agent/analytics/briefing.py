"""Leadership-brief assembly (FR-17, optional).

The brief says "help prepare data for leadership updates" without defining the shape of
one; plan section 9.1/the Decision Log interpret it as: compose the metrics F05 already
proved correct into one structured brief, plus a ready-to-paste Markdown summary, with
zero new arithmetic. Every number here is a `MetricResult` this module did not compute —
`build_leadership_brief` is assembly, not analysis, matching plan section 3.2's rule that
the model (and, by the same logic, this layer) never performs arithmetic of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from bi_agent.analytics.metrics import (
    collected_amount,
    pipeline_value,
    receivable,
    revenue_billed,
    sector_breakdown,
    stage_distribution,
)
from bi_agent.analytics.spec import MetricResult
from bi_agent.data.repository import BoardData

__all__ = ["LeadershipBrief", "build_leadership_brief"]

#: How many sectors the brief names by pipeline value. A judgment call, not a measured
#: number - short enough to read in one glance, long enough to not hide the #2/#3 sector.
_TOP_N_SECTORS = 5


@dataclass(frozen=True)
class LeadershipBrief:
    #: The period phrase as given (e.g. "this quarter"), or `None` for all-time. Not the
    #: resolved date range - `pipeline`/`revenue_billed` already carry any fallback
    #: caveat if the requested period had no rows (F05 section 3.7).
    period_label: str | None
    pipeline: MetricResult
    revenue_billed: MetricResult
    collected: MetricResult
    receivable: MetricResult
    stage_distribution: dict[str, MetricResult]
    #: `(sector, MetricResult)`, sorted by pipeline value descending. Always board-wide -
    #: computed before any `sector` filter, since ranking one sector against itself is
    #: not useful.
    top_sectors_by_pipeline: list[tuple[str, MetricResult]]
    stage_status_conflicts: int
    data_quality_caveats: list[str]
    markdown: str


def _top_sectors(deals: BoardData, *, n: int) -> list[tuple[str, MetricResult]]:
    grouped = sector_breakdown(deals, board="deals", metric="sum", field="deal_value")
    ranked = sorted(
        ((str(key), result) for key, result in grouped.items() if result.value is not None),
        key=lambda pair: pair[1].value,
        reverse=True,
    )
    return ranked[:n]


def _data_quality_caveats(deals: BoardData, work_orders: BoardData) -> list[str]:
    caveats: list[str] = []
    for label, data in (("Deals", deals), ("Work Orders", work_orders)):
        always_null = data.quality.always_null_fields()
        if always_null:
            caveats.append(f"{label}: always empty on every record - {', '.join(always_null)}.")
    if deals.quality.stage_status_conflicts:
        caveats.append(
            f"{deals.quality.stage_status_conflicts} deals are marked Won at a stage that "
            "does not support it yet - Deal Status and Deal Stage disagree (CLAUDE.md)."
        )
    if deals.quality.n_junk_rows_excluded:
        caveats.append(
            f"{deals.quality.n_junk_rows_excluded} data-entry error row(s) excluded from "
            "every Deals figure in this brief."
        )
    return caveats


def _fmt_money(result: MetricResult) -> str:
    if result.value is None:
        return "no data recorded"
    return f"Rs {result.value:,.0f} ({result.n_used} of {result.n_total} rows)"


def _render_markdown(
    *,
    period_label: str | None,
    pipeline: MetricResult,
    revenue: MetricResult,
    collected: MetricResult,
    receivable_result: MetricResult,
    stages: dict[str, MetricResult],
    top_sectors: list[tuple[str, MetricResult]],
    quality_caveats: list[str],
) -> str:
    lines = [f"# Leadership Update — {period_label or 'All-time'}", ""]

    lines.append("## Pipeline")
    lines.append(f"- Pipeline value (deal-value basis): {_fmt_money(pipeline)}")
    lines.extend(f"  - {caveat}" for caveat in pipeline.caveats)
    lines.append("")

    lines.append("## Revenue & Collections")
    lines.append(f"- Billed (revenue): {_fmt_money(revenue)}")
    lines.append(f"- Collected (cash): {_fmt_money(collected)}")
    lines.append(f"- Outstanding receivable: {_fmt_money(receivable_result)}")
    for result in (revenue, collected, receivable_result):
        lines.extend(f"  - {caveat}" for caveat in result.caveats)
    lines.append("")

    lines.append("## Top Sectors by Pipeline Value")
    if top_sectors:
        for name, result in top_sectors:
            lines.append(f"- {name}: Rs {result.value:,.0f}")
    else:
        lines.append("- No sector carries a recorded deal value.")
    lines.append("")

    lines.append("## Deal Stage Distribution")
    for stage, result in sorted(stages.items(), key=lambda kv: str(kv[0])):
        lines.append(f"- {stage}: {result.value}")
    lines.append("")

    lines.append("## Data Quality Notes")
    if quality_caveats:
        lines.extend(f"- {caveat}" for caveat in quality_caveats)
    else:
        lines.append("- No material data-quality issues affecting this brief.")

    return "\n".join(lines)


def build_leadership_brief(
    deals: BoardData,
    work_orders: BoardData,
    *,
    sector: str | None = None,
    period: str | None = None,
    now: date | None = None,
) -> LeadershipBrief:
    pipeline = pipeline_value(deals, sector=sector, period=period, now=now)
    revenue = revenue_billed(work_orders, sector=sector, period=period, now=now)
    collected = collected_amount(work_orders)
    receivable_result = receivable(work_orders)
    stages = stage_distribution(deals)
    top_sectors = _top_sectors(deals, n=_TOP_N_SECTORS)
    quality_caveats = _data_quality_caveats(deals, work_orders)

    markdown = _render_markdown(
        period_label=period,
        pipeline=pipeline,
        revenue=revenue,
        collected=collected,
        receivable_result=receivable_result,
        stages=stages,
        top_sectors=top_sectors,
        quality_caveats=quality_caveats,
    )

    return LeadershipBrief(
        period_label=period,
        pipeline=pipeline,
        revenue_billed=revenue,
        collected=collected,
        receivable=receivable_result,
        stage_distribution=stages,
        top_sectors_by_pipeline=top_sectors,
        stage_status_conflicts=deals.quality.stage_status_conflicts,
        data_quality_caveats=quality_caveats,
        markdown=markdown,
    )
