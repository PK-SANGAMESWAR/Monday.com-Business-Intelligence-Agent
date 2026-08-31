"""Tests for bi_agent/analytics/briefing.py — leadership-brief assembly (F08, FR-17)."""

from __future__ import annotations

from datetime import date

import pytest

from bi_agent.analytics.briefing import build_leadership_brief
from bi_agent.data.normalize import normalize_deals, normalize_work_orders
from bi_agent.data.quality import build_quality_report
from bi_agent.data.repository import BoardData
from bi_agent.data.schema import DEALS_FIELDS, WORK_ORDERS_FIELDS


@pytest.fixture
def deals_data(deals_snapshot) -> BoardData:
    normalized = normalize_deals(deals_snapshot)
    quality = build_quality_report(normalized, DEALS_FIELDS)
    return BoardData(normalized=normalized, quality=quality, fetched_at=None)


@pytest.fixture
def work_orders_data(work_orders_snapshot) -> BoardData:
    normalized = normalize_work_orders(work_orders_snapshot)
    quality = build_quality_report(normalized, WORK_ORDERS_FIELDS)
    return BoardData(normalized=normalized, quality=quality, fetched_at=None)


def test_brief_figures_match_the_verified_golden_values(deals_data, work_orders_data):
    """Same figures as test_metrics.py's golden-value tests - a brief must never
    recompute, only assemble, so these must match exactly."""
    brief = build_leadership_brief(deals_data, work_orders_data)
    assert brief.pipeline.value == pytest.approx(2_305_518_040.91, abs=0.01)
    assert brief.pipeline.basis == "deal_value"
    assert brief.revenue_billed.value == pytest.approx(126_719_936.37, abs=0.01)
    assert brief.revenue_billed.basis == "billed"
    assert brief.collected.value == pytest.approx(90_428_187.50, abs=0.01)
    assert brief.receivable.value == pytest.approx(36_291_748.87, abs=0.01)


def test_brief_stage_distribution_covers_every_non_junk_deal(deals_data, work_orders_data):
    brief = build_leadership_brief(deals_data, work_orders_data)
    assert sum(r.value for r in brief.stage_distribution.values()) == 344


def test_brief_top_sectors_sorted_descending_and_capped(deals_data, work_orders_data):
    brief = build_leadership_brief(deals_data, work_orders_data)
    values = [result.value for _, result in brief.top_sectors_by_pipeline]
    assert values == sorted(values, reverse=True)
    assert len(brief.top_sectors_by_pipeline) <= 5


def test_brief_surfaces_stage_status_conflicts(deals_data, work_orders_data):
    brief = build_leadership_brief(deals_data, work_orders_data)
    assert brief.stage_status_conflicts == 72
    assert any("marked Won" in c for c in brief.data_quality_caveats)


def test_brief_surfaces_always_null_work_order_fields(deals_data, work_orders_data):
    brief = build_leadership_brief(deals_data, work_orders_data)
    assert any(
        "collection_date" in c and "Work Orders" in c for c in brief.data_quality_caveats
    )


def test_brief_sector_filter_scopes_pipeline_and_revenue_only(deals_data, work_orders_data):
    """Filtering by sector narrows the pipeline/revenue figures but top-sector ranking
    stays board-wide - ranking a single sector against itself would be meaningless."""
    filtered = build_leadership_brief(deals_data, work_orders_data, sector="Mining")
    unfiltered = build_leadership_brief(deals_data, work_orders_data)
    assert filtered.pipeline.n_total < unfiltered.pipeline.n_total
    assert len(filtered.top_sectors_by_pipeline) == len(unfiltered.top_sectors_by_pipeline)


def test_brief_period_with_no_data_states_the_fallback(deals_data, work_orders_data):
    brief = build_leadership_brief(
        deals_data, work_orders_data, period="this quarter", now=date(2026, 8, 31)
    )
    assert any("most recent period with data" in c for c in brief.pipeline.caveats)


def test_brief_markdown_contains_every_section_and_no_bare_figures(deals_data, work_orders_data):
    brief = build_leadership_brief(deals_data, work_orders_data)
    for heading in (
        "# Leadership Update",
        "## Pipeline",
        "## Revenue & Collections",
        "## Top Sectors by Pipeline Value",
        "## Deal Stage Distribution",
        "## Data Quality Notes",
    ):
        assert heading in brief.markdown
    # The pipeline coverage caveat must ride along into the rendered text, not just the
    # structured MetricResult - a founder reading only the Markdown must still see it.
    assert "165 of 344" in brief.markdown


def test_brief_all_time_label_when_no_period_given(deals_data, work_orders_data):
    brief = build_leadership_brief(deals_data, work_orders_data)
    assert brief.period_label is None
    assert "All-time" in brief.markdown
