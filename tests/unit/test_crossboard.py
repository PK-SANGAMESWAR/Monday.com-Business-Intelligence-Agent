"""Tests for bi_agent/analytics/crossboard.py — side-by-side comparison, join refusal."""

from __future__ import annotations

import pytest

from bi_agent.analytics.crossboard import CROSSBOARD_DIMENSIONS, compare_boards
from bi_agent.data.normalize import normalize_deals, normalize_work_orders
from bi_agent.data.quality import build_quality_report
from bi_agent.data.repository import BoardData
from bi_agent.data.schema import DEALS_FIELDS, WORK_ORDERS_FIELDS
from bi_agent.errors import QuerySpecError


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


def test_compare_boards_on_sector_returns_both_sides(deals_data, work_orders_data):
    comparison = compare_boards(deals_data, work_orders_data, dimension="sector")
    assert comparison.dimension == "sector"
    assert sum(r.value for r in comparison.deals.values() if r.value is not None) == pytest.approx(
        2_305_518_040.91, abs=0.01
    )
    assert set(comparison.deals) != set(comparison.work_orders)


def test_compare_boards_reports_asymmetric_sectors(deals_data, work_orders_data):
    """Deals carry 12 sectors, Work Orders 6 (CLAUDE.md) - Aviation is deals-only."""
    comparison = compare_boards(deals_data, work_orders_data, dimension="sector")
    assert "Aviation" in comparison.deals_only_keys
    assert any("seen only in Deals" in c for c in comparison.caveats)


def test_compare_boards_never_a_row_join_caveat_always_present(deals_data, work_orders_data):
    comparison = compare_boards(deals_data, work_orders_data, dimension="owner_code")
    assert any("not a row-level join" in c for c in comparison.caveats)


def test_compare_boards_rejects_deal_name_join(deals_data, work_orders_data):
    """The join-refusal policy (OQ-7): asking to compare on `deal_name` is really asking
    for a row join and must fail loudly, naming why, not silently group by a field that
    happens to exist on both boards."""
    with pytest.raises(QuerySpecError) as exc_info:
        compare_boards(deals_data, work_orders_data, dimension="deal_name")
    assert "Sakura" in exc_info.value.hint
    assert "no reliable row-level key" in exc_info.value.hint


def test_compare_boards_rejects_unknown_dimension(deals_data, work_orders_data):
    with pytest.raises(QuerySpecError) as exc_info:
        compare_boards(deals_data, work_orders_data, dimension="serial_no")
    assert "sector" in exc_info.value.hint
    assert "owner_code" in exc_info.value.hint


def test_crossboard_dimensions_are_shared_canonical_fields():
    deals_fields = {spec.canonical for spec in DEALS_FIELDS}
    wo_fields = {spec.canonical for spec in WORK_ORDERS_FIELDS}
    assert CROSSBOARD_DIMENSIONS <= (deals_fields & wo_fields)


def test_compare_boards_owner_code_counts(deals_data, work_orders_data):
    """OWNER_008 exists on Work Orders but not Deals (CLAUDE.md)."""
    comparison = compare_boards(
        deals_data, work_orders_data, dimension="owner_code", deals_metric="count",
        deals_field=None, wo_metric="count", wo_field=None,
    )
    assert comparison.work_orders_only_keys == ["OWNER_008"]
