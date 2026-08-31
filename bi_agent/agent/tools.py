"""Tool schemas exposed to the model, and the dispatcher that backs them with F05.

The model never computes (plan section 3.2): every numeric field in a tool result is a
`MetricResult` produced by `bi_agent.analytics`, serialized as-is. `dispatch_tool` is the
only place this package turns a model-chosen tool name into a function call, and it never
raises `QuerySpecError` outward — F01 designed that exception to be handed back to the
model as a correctable tool result, so a bad field name becomes a retry, not a crash.

`compare_boards` (F07) refuses row-level joins structurally: it only accepts a dimension
shared by both boards under the same canonical name, so a request that is really asking
for a row join fails as a correctable tool error, never as a silently wrong number.
`leadership_brief` (F08) composes the same tested metrics into one founder-ready summary;
nothing in either tool performs fresh arithmetic.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from bi_agent.analytics.briefing import build_leadership_brief
from bi_agent.analytics.crossboard import compare_boards
from bi_agent.analytics.metrics import (
    collected_amount,
    pipeline_value,
    receivable,
    revenue_billed,
    run_query,
    stage_distribution,
)
from bi_agent.analytics.spec import Filter, MetricResult, QuerySpec
from bi_agent.data.repository import BoardData, BoardRepository
from bi_agent.data.schema import DEALS_FIELDS, WORK_ORDERS_FIELDS
from bi_agent.errors import QuerySpecError

__all__ = ["TOOL_SCHEMAS", "dispatch_tool"]

_FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "op": {
            "type": "string",
            "enum": ["eq", "ne", "in", "gt", "gte", "lt", "lte", "is_null", "not_null"],
        },
        "value": {},
    },
    "required": ["field", "op"],
}

_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "filters": {"type": "array", "items": _FILTER_SCHEMA},
        "group_by": {"type": "array", "items": {"type": "string"}},
        "metric": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
        "field": {
            "type": "string",
            "description": "Required unless metric is 'count'. Call describe_data first "
            "to see valid field names.",
        },
    },
    "required": ["metric"],
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "describe_data",
        "description": (
            "Board shape for 'deals' or 'work_orders': every field, its type, coverage "
            "(how many of the non-junk rows have a value), whether it is always empty, "
            "and sample observed values for text fields. Call this before guessing a "
            "field name or a category spelling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"board": {"type": "string", "enum": ["deals", "work_orders"]}},
            "required": ["board"],
        },
    },
    {
        "name": "query_deals",
        "description": (
            "Validated filter/group_by/metric query over the Deals board. Junk "
            "data-entry rows are always excluded automatically."
        ),
        "input_schema": _QUERY_SCHEMA,
    },
    {
        "name": "query_work_orders",
        "description": "Validated filter/group_by/metric query over the Work Orders board.",
        "input_schema": _QUERY_SCHEMA,
    },
    {
        "name": "pipeline_health",
        "description": (
            "Composite pipeline view: total pipeline value (deal_value basis), stage "
            "distribution, and the count of deals where Deal Status contradicts Deal "
            "Stage (e.g. marked Won while still at an early stage)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string"},
                "period": {
                    "type": "string",
                    "description": "'this quarter', 'last quarter', 'this fiscal year', "
                    "'last fiscal year', or 'FY25-26'/'FY25-26 Q3'.",
                },
            },
        },
    },
    {
        "name": "revenue_and_collections",
        "description": (
            "Billed revenue, collected cash and outstanding receivable for Work Orders, "
            "each with its own coverage and caveats. These are three different figures "
            "(billed != collected != pipeline) - never conflate them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string"},
                "period": {"type": "string"},
            },
        },
    },
    {
        "name": "data_quality_report",
        "description": "What is missing, contradictory, or unanswerable on a board, and why.",
        "input_schema": {
            "type": "object",
            "properties": {"board": {"type": "string", "enum": ["deals", "work_orders"]}},
            "required": ["board"],
        },
    },
    {
        "name": "compare_boards",
        "description": (
            "Side-by-side comparison of Deals and Work Orders on a shared dimension. "
            "Never a row-level join - Deals and Work Orders share no reliable key, so "
            "each board is aggregated independently and the two results are returned "
            "next to each other, plus which dimension values exist on only one board. "
            "Asking for any dimension other than 'sector' or 'owner_code' (e.g. "
            "'deal_name') is refused - that would really be a row join."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": ["sector", "owner_code"]},
                "deals_metric": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
                "deals_field": {"type": "string", "description": "Default 'deal_value'."},
                "wo_metric": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
                "wo_field": {"type": "string", "description": "Default 'billed_incl_gst'."},
            },
            "required": ["dimension"],
        },
    },
    {
        "name": "leadership_brief",
        "description": (
            "Assembles a founder-ready leadership update: pipeline value, billed "
            "revenue, collected cash, receivable, deal-stage distribution, top sectors "
            "by pipeline value, and data-quality caveats - as structured figures plus a "
            "ready-to-paste Markdown summary. Every figure is the same tested metric "
            "used by the other tools; nothing here is freshly computed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "Filters pipeline/revenue only; top-sector ranking stays board-wide."},
                "period": {
                    "type": "string",
                    "description": "'this quarter', 'last quarter', 'this fiscal year', "
                    "'last fiscal year', or 'FY25-26'/'FY25-26 Q3'.",
                },
            },
        },
    },
]


def _metric_result_to_dict(result: MetricResult) -> dict[str, Any]:
    return asdict(result)


def _board_data(board: str, repository: BoardRepository) -> BoardData:
    return repository.deals() if board == "deals" else repository.work_orders()


def _describe_data(board: str, repository: BoardRepository) -> dict[str, Any]:
    data = _board_data(board, repository)
    fields = DEALS_FIELDS if board == "deals" else WORK_ORDERS_FIELDS
    frame = data.frame

    described: dict[str, Any] = {}
    for spec in fields:
        entry: dict[str, Any] = {
            "type": spec.field_type,
            "always_null": spec.always_null,
            "summable": spec.summable,
        }
        coverage = data.quality.coverage.get(spec.canonical)
        if coverage is not None:
            entry["n_present"] = coverage.n_present
            entry["n_total"] = coverage.n_total
        if spec.field_type == "text" and spec.canonical in frame.columns:
            values = sorted(str(v) for v in frame[spec.canonical].dropna().unique())
            if 0 < len(values) <= 40:
                entry["sample_values"] = values
        described[spec.canonical] = entry

    return {
        "board": board,
        "n_rows": data.quality.n_total_rows,
        "n_junk_rows_excluded": data.quality.n_junk_rows_excluded,
        "fields": described,
        "always_null_fields": data.quality.always_null_fields(),
    }


def _run_query_tool(board: str, arguments: dict[str, Any], repository: BoardRepository) -> dict[str, Any]:
    data = _board_data(board, repository)
    spec = QuerySpec(
        board=board,
        filters=[Filter(**f) for f in arguments.get("filters", [])],
        group_by=arguments.get("group_by", []),
        metric=arguments["metric"],
        field=arguments.get("field"),
    )
    result = run_query(spec, data)
    if isinstance(result, dict):
        return {"grouped": {str(key): _metric_result_to_dict(value) for key, value in result.items()}}
    return _metric_result_to_dict(result)


def _pipeline_health(arguments: dict[str, Any], repository: BoardRepository) -> dict[str, Any]:
    data = repository.deals()
    value = pipeline_value(data, sector=arguments.get("sector"), period=arguments.get("period"))
    stages = stage_distribution(data)
    return {
        "pipeline_value": _metric_result_to_dict(value),
        "stage_distribution": {str(key): _metric_result_to_dict(v) for key, v in stages.items()},
        "stage_status_conflicts": data.quality.stage_status_conflicts,
    }


def _revenue_and_collections(arguments: dict[str, Any], repository: BoardRepository) -> dict[str, Any]:
    data = repository.work_orders()
    return {
        "revenue_billed": _metric_result_to_dict(
            revenue_billed(data, sector=arguments.get("sector"), period=arguments.get("period"))
        ),
        "collected": _metric_result_to_dict(collected_amount(data)),
        "receivable": _metric_result_to_dict(receivable(data)),
    }


def _data_quality_report(board: str, repository: BoardRepository) -> dict[str, Any]:
    quality = _board_data(board, repository).quality
    return {
        "board": quality.board,
        "n_total_rows": quality.n_total_rows,
        "n_junk_rows_excluded": quality.n_junk_rows_excluded,
        "stage_status_conflicts": quality.stage_status_conflicts,
        "casing_fixes": quality.casing_fixes,
        "always_null_fields": quality.always_null_fields(),
        "sparse_fields": quality.sparse_fields(),
        "coverage": {
            name: {
                "n_total": cov.n_total,
                "n_present": cov.n_present,
                "n_unrepresentable": cov.n_unrepresentable,
                "coverage_ratio": round(cov.coverage_ratio, 3),
            }
            for name, cov in quality.coverage.items()
        },
    }


def _compare_boards_tool(arguments: dict[str, Any], repository: BoardRepository) -> dict[str, Any]:
    comparison = compare_boards(
        repository.deals(),
        repository.work_orders(),
        dimension=arguments["dimension"],
        deals_metric=arguments.get("deals_metric", "sum"),
        deals_field=arguments.get("deals_field", "deal_value"),
        wo_metric=arguments.get("wo_metric", "sum"),
        wo_field=arguments.get("wo_field", "billed_incl_gst"),
    )
    return {
        "dimension": comparison.dimension,
        "deals": {key: _metric_result_to_dict(value) for key, value in comparison.deals.items()},
        "work_orders": {
            key: _metric_result_to_dict(value) for key, value in comparison.work_orders.items()
        },
        "deals_only_keys": comparison.deals_only_keys,
        "work_orders_only_keys": comparison.work_orders_only_keys,
        "caveats": comparison.caveats,
    }


def _leadership_brief_tool(arguments: dict[str, Any], repository: BoardRepository) -> dict[str, Any]:
    brief = build_leadership_brief(
        repository.deals(),
        repository.work_orders(),
        sector=arguments.get("sector"),
        period=arguments.get("period"),
    )
    return {
        "period_label": brief.period_label,
        "pipeline": _metric_result_to_dict(brief.pipeline),
        "revenue_billed": _metric_result_to_dict(brief.revenue_billed),
        "collected": _metric_result_to_dict(brief.collected),
        "receivable": _metric_result_to_dict(brief.receivable),
        "stage_distribution": {
            str(key): _metric_result_to_dict(value) for key, value in brief.stage_distribution.items()
        },
        "top_sectors_by_pipeline": [
            {"sector": name, **_metric_result_to_dict(result)}
            for name, result in brief.top_sectors_by_pipeline
        ],
        "stage_status_conflicts": brief.stage_status_conflicts,
        "data_quality_caveats": brief.data_quality_caveats,
        "markdown": brief.markdown,
    }


def dispatch_tool(
    name: str, arguments: dict[str, Any], *, repository: BoardRepository
) -> dict[str, Any]:
    """Run one tool call. Never raises - a malformed or invalid call becomes a
    correctable `{"error": ..., "hint": ...}` result the model can retry from, not a
    crash of the whole conversation."""
    try:
        if name == "describe_data":
            return _describe_data(arguments["board"], repository)
        if name == "query_deals":
            return _run_query_tool("deals", arguments, repository)
        if name == "query_work_orders":
            return _run_query_tool("work_orders", arguments, repository)
        if name == "pipeline_health":
            return _pipeline_health(arguments, repository)
        if name == "revenue_and_collections":
            return _revenue_and_collections(arguments, repository)
        if name == "data_quality_report":
            return _data_quality_report(arguments["board"], repository)
        if name == "compare_boards":
            return _compare_boards_tool(arguments, repository)
        if name == "leadership_brief":
            return _leadership_brief_tool(arguments, repository)
        return {
            "error": f"unknown tool {name!r}",
            "hint": "This tool does not exist. Choose one of the tools provided.",
        }
    except QuerySpecError as exc:
        return {"error": str(exc), "hint": exc.hint}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "error": f"invalid arguments for {name!r}: {exc}",
            "hint": "Check the tool's input schema and retry with corrected arguments.",
        }
