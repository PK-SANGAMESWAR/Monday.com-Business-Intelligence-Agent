"""Tests for bi_agent/agent/tools.py — the dispatcher backing the model's tool calls."""

from __future__ import annotations

import pytest

from bi_agent.agent.tools import TOOL_SCHEMAS, dispatch_tool


def test_tool_schemas_are_well_formed():
    names = [schema["name"] for schema in TOOL_SCHEMAS]
    assert len(names) == len(set(names))
    for schema in TOOL_SCHEMAS:
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"


def test_all_tools_are_exposed():
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {
        "describe_data",
        "query_deals",
        "query_work_orders",
        "pipeline_health",
        "revenue_and_collections",
        "data_quality_report",
        "compare_boards",
        "leadership_brief",
    }


def test_describe_data_deals(board_repository):
    result = dispatch_tool("describe_data", {"board": "deals"}, repository=board_repository)
    assert result["board"] == "deals"
    assert result["n_rows"] == 344
    assert result["n_junk_rows_excluded"] == 2
    assert "deal_value" in result["fields"]
    assert "Mining" in result["fields"]["sector"]["sample_values"]


def test_describe_data_work_orders_flags_always_null_fields(board_repository):
    result = dispatch_tool("describe_data", {"board": "work_orders"}, repository=board_repository)
    assert set(result["always_null_fields"]) == {
        "expected_billing_month",
        "actual_collection_month",
        "collection_status",
        "collection_date",
    }


def test_query_deals_sum_matches_verified_figure(board_repository):
    result = dispatch_tool(
        "query_deals",
        {"metric": "sum", "field": "deal_value"},
        repository=board_repository,
    )
    assert result["value"] == pytest.approx(2_305_518_040.91, abs=0.01)
    assert result["n_used"] == 165
    assert result["excluded"]["junk_row"] == 2


def test_query_deals_invalid_field_returns_correctable_error_not_a_crash(board_repository):
    result = dispatch_tool(
        "query_deals", {"metric": "count", "filters": [{"field": "nope", "op": "eq", "value": "x"}]},
        repository=board_repository,
    )
    assert "error" in result
    assert "hint" in result
    assert result["hint"]


def test_query_work_orders_grouped_shape(board_repository):
    result = dispatch_tool(
        "query_work_orders",
        {"metric": "count", "group_by": ["sector"]},
        repository=board_repository,
    )
    assert "grouped" in result
    assert sum(v["value"] for v in result["grouped"].values()) == 176


def test_pipeline_health_composite(board_repository):
    result = dispatch_tool("pipeline_health", {}, repository=board_repository)
    assert result["pipeline_value"]["value"] == pytest.approx(2_305_518_040.91, abs=0.01)
    assert result["stage_status_conflicts"] == 72
    assert sum(v["value"] for v in result["stage_distribution"].values()) == 344


def test_revenue_and_collections_composite(board_repository):
    result = dispatch_tool("revenue_and_collections", {}, repository=board_repository)
    assert result["revenue_billed"]["basis"] == "billed"
    assert result["collected"]["basis"] == "collected"
    assert result["receivable"]["basis"] == "receivable"
    assert any("Collection Date is empty" in c for c in result["collected"]["caveats"])


def test_data_quality_report_work_orders(board_repository):
    result = dispatch_tool("data_quality_report", {"board": "work_orders"}, repository=board_repository)
    assert result["casing_fixes"] == {"BIlled -> Billed": 3}
    assert "ar_priority" in result["sparse_fields"]


def test_compare_boards_sector_dimension(board_repository):
    result = dispatch_tool("compare_boards", {"dimension": "sector"}, repository=board_repository)
    assert result["dimension"] == "sector"
    assert "Aviation" in result["deals_only_keys"]
    assert any("not a row-level join" in c for c in result["caveats"])


def test_compare_boards_rejects_join_dimension(board_repository):
    result = dispatch_tool(
        "compare_boards", {"dimension": "deal_name"}, repository=board_repository
    )
    assert "error" in result
    assert "Sakura" in result["hint"]


def test_leadership_brief_composes_verified_figures(board_repository):
    result = dispatch_tool("leadership_brief", {}, repository=board_repository)
    assert result["pipeline"]["value"] == pytest.approx(2_305_518_040.91, abs=0.01)
    assert result["revenue_billed"]["basis"] == "billed"
    assert result["stage_status_conflicts"] == 72
    assert "# Leadership Update" in result["markdown"]
    assert len(result["top_sectors_by_pipeline"]) <= 5


def test_unknown_tool_returns_correctable_error(board_repository):
    result = dispatch_tool("not_a_real_tool", {}, repository=board_repository)
    assert "error" in result


def test_missing_required_argument_returns_correctable_error(board_repository):
    result = dispatch_tool("describe_data", {}, repository=board_repository)
    assert "error" in result
    assert "hint" in result
