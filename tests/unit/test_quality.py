"""Tests for bi_agent/data/quality.py."""

from __future__ import annotations

from bi_agent.data.normalize import normalize_deals, normalize_work_orders
from bi_agent.data.quality import build_quality_report
from bi_agent.data.schema import DEALS_FIELDS, WORK_ORDERS_FIELDS


def test_deals_report_excludes_junk_from_totals(deals_snapshot):
    normalized = normalize_deals(deals_snapshot)
    report = build_quality_report(normalized, DEALS_FIELDS)

    assert report.n_junk_rows_excluded == 2
    assert report.n_total_rows == 344
    # Coverage denominators must exclude the 2 junk rows, or every ratio is polluted by
    # rows that were never real deals.
    assert report.coverage["deal_value"].n_total == 344


def test_deals_report_stage_status_conflicts_matches_data_profile(deals_snapshot):
    normalized = normalize_deals(deals_snapshot)
    report = build_quality_report(normalized, DEALS_FIELDS)
    assert report.stage_status_conflicts == 72


def test_work_orders_always_null_fields_flagged(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    report = build_quality_report(normalized, WORK_ORDERS_FIELDS)

    always_null = set(report.always_null_fields())
    assert always_null == {
        "expected_billing_month",
        "actual_collection_month",
        "collection_status",
        "collection_date",
    }
    for name in always_null:
        assert report.coverage[name].n_present == 0


def test_work_orders_casing_fixes_reported(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    report = build_quality_report(normalized, WORK_ORDERS_FIELDS)
    assert report.casing_fixes == {"BIlled -> Billed": 3}


def test_field_coverage_ratio_and_missing():
    from bi_agent.data.quality import FieldCoverage

    cov = FieldCoverage(field="x", n_total=100, n_present=60, n_unrepresentable=5, always_null=False)
    assert cov.n_missing == 35
    assert cov.coverage_ratio == 0.6


def test_sparse_fields_excludes_always_null(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    report = build_quality_report(normalized, WORK_ORDERS_FIELDS)

    sparse = report.sparse_fields(threshold=0.2)
    # The always-null fields are 0% covered but must not appear here - they get their own,
    # stronger statement via always_null_fields().
    assert not set(sparse) & set(report.always_null_fields())
    assert "ar_priority" in sparse  # 10/176 in DATA_PROFILE.md
