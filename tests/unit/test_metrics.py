"""Golden-value tests for bi_agent/analytics/metrics.py, against the real seeded boards.

Deal-value sum and Work Orders money-column sums are cross-checked against the exact
figures F03's own live verification proved round-tripped correctly
(docs/features/03_BOARD_SEEDING.md section 9) — an independent confirmation that
normalization did not silently lose or duplicate anything on the way to a metric.
"""

from __future__ import annotations

from datetime import date

import pytest

from bi_agent.analytics.metrics import (
    always_null_fields_report,
    collected_amount,
    pipeline_value,
    receivable,
    revenue_billed,
    run_query,
    sector_breakdown,
    stage_distribution,
)
from bi_agent.analytics.spec import Filter, QuerySpec
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


# --- run_query: the generic engine --------------------------------------------------


def test_count_all_deals_excludes_junk_rows(deals_data):
    spec = QuerySpec(board="deals", metric="count")
    result = run_query(spec, deals_data)
    assert result.value == 344
    assert result.excluded == {"junk_row": 2}
    assert any("data-entry error" in c for c in result.caveats)


def test_sum_deal_value_matches_f03_verified_figure(deals_data):
    spec = QuerySpec(board="deals", metric="sum", field="deal_value")
    result = run_query(spec, deals_data)
    assert result.n_used == 165
    assert result.n_total == 344
    assert result.value == pytest.approx(2_305_518_040.91, abs=0.01)
    assert result.excluded["value_missing"] == 344 - 165
    assert any("165 of 344" in c for c in result.caveats)


def test_group_by_sector_partitions_correctly(deals_data):
    spec = QuerySpec(board="deals", group_by=["sector"], metric="count")
    grouped = run_query(spec, deals_data)
    assert isinstance(grouped, dict)
    assert sum(r.value for r in grouped.values()) == 344


def test_filter_by_status_eq_won(deals_data):
    spec = QuerySpec(board="deals", filters=[Filter(field="status", op="eq", value="Won")], metric="count")
    result = run_query(spec, deals_data)
    assert result.value == 165


def test_unknown_filter_field_raises_query_spec_error(deals_data):
    spec = QuerySpec(board="deals", filters=[Filter(field="not_a_field", op="eq", value="x")], metric="count")
    with pytest.raises(QuerySpecError):
        run_query(spec, deals_data)


def test_sum_on_non_summable_quantity_field_is_refused(work_orders_data):
    spec = QuerySpec(board="work_orders", metric="sum", field="qty_po_raw")
    with pytest.raises(QuerySpecError, match="not summable"):
        run_query(spec, work_orders_data)


# --- named metrics --------------------------------------------------------------------


def test_pipeline_value_basis_is_deal_value(deals_data):
    result = pipeline_value(deals_data)
    assert result.basis == "deal_value"
    assert result.value == pytest.approx(2_305_518_040.91, abs=0.01)


def test_pipeline_value_filtered_by_sector(deals_data):
    result = pipeline_value(deals_data, sector="Mining")
    assert result.n_total < 344
    assert result.value is not None


def test_pipeline_value_period_with_no_data_falls_back_and_says_so(deals_data):
    """`now` = 2026-08-31; the workbook's dates top out 2026-01, so "this quarter" (Jul-Sep
    2026) has no rows at all - the fallback and caveat are the whole point of this test."""
    result = pipeline_value(deals_data, period="this quarter", now=date(2026, 8, 31))
    assert any("most recent period with data" in c for c in result.caveats)


def test_revenue_billed_basis_and_amount(work_orders_data):
    result = revenue_billed(work_orders_data)
    assert result.basis == "billed"
    assert result.value == pytest.approx(126_719_936.37, abs=0.01)
    assert any("Basis: billed value" in c for c in result.caveats)


def test_collected_amount_states_the_collection_timing_limitation(work_orders_data):
    result = collected_amount(work_orders_data)
    assert result.basis == "collected"
    assert result.value == pytest.approx(90_428_187.50, abs=0.01)
    assert any("Collection Date is empty" in c for c in result.caveats)


def test_receivable_amount(work_orders_data):
    result = receivable(work_orders_data)
    assert result.basis == "receivable"
    assert result.value == pytest.approx(36_291_748.87, abs=0.01)


def test_stage_distribution_sums_to_non_junk_total(deals_data):
    grouped = stage_distribution(deals_data)
    assert sum(r.value for r in grouped.values()) == 344


def test_sector_breakdown_per_board(deals_data, work_orders_data):
    deals_sectors = sector_breakdown(deals_data, board="deals", metric="count")
    wo_sectors = sector_breakdown(work_orders_data, board="work_orders", metric="count")
    # The two boards carry different sector vocabularies (CLAUDE.md) - never merged.
    assert set(deals_sectors) != set(wo_sectors) or len(deals_sectors) != len(wo_sectors)


def test_always_null_fields_report(work_orders_data):
    fields = always_null_fields_report(work_orders_data)
    assert set(fields) == {
        "expected_billing_month",
        "actual_collection_month",
        "collection_status",
        "collection_date",
    }
