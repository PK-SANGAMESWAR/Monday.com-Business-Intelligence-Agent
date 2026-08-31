"""Tests for bi_agent/analytics/spec.py — QuerySpec validation."""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from bi_agent.analytics.spec import Filter, QuerySpec, validate_categorical_values, validate_spec
from bi_agent.errors import QuerySpecError


def test_valid_spec_passes():
    spec = QuerySpec(board="deals", metric="sum", field="deal_value")
    validate_spec(spec)  # must not raise


def test_count_needs_no_field():
    spec = QuerySpec(board="deals", metric="count")
    validate_spec(spec)


def test_unknown_filter_field_is_rejected():
    spec = QuerySpec(board="deals", filters=[Filter(field="not_a_field", op="eq", value="x")], metric="count")
    with pytest.raises(QuerySpecError) as excinfo:
        validate_spec(spec)
    assert "not_a_field" in str(excinfo.value)
    assert excinfo.value.hint  # a model-addressed hint must exist


def test_unknown_group_by_field_is_rejected():
    spec = QuerySpec(board="deals", group_by=["not_a_field"], metric="count")
    with pytest.raises(QuerySpecError):
        validate_spec(spec)


def test_sum_requires_a_field():
    spec = QuerySpec(board="deals", metric="sum")
    with pytest.raises(QuerySpecError, match="requires 'field'"):
        validate_spec(spec)


def test_sum_on_a_non_numeric_field_is_rejected():
    spec = QuerySpec(board="deals", metric="sum", field="sector")
    with pytest.raises(QuerySpecError, match="not numeric"):
        validate_spec(spec)


def test_sum_on_a_non_summable_quantity_field_is_rejected():
    """CLAUDE.md: mixed-unit quantities are not summable — refuse, name why."""
    spec = QuerySpec(board="work_orders", metric="sum", field="qty_po_raw")
    with pytest.raises(QuerySpecError, match="not summable"):
        validate_spec(spec)


def test_avg_min_max_also_check_summability():
    for metric in ("avg", "min", "max"):
        spec = QuerySpec(board="work_orders", metric=metric, field="qty_po_raw")
        with pytest.raises(QuerySpecError):
            validate_spec(spec)


def test_in_operator_requires_a_list_value():
    with pytest.raises(ValidationError):
        Filter(field="sector", op="in", value="Mining")


def test_in_operator_accepts_a_list_value():
    filt = Filter(field="sector", op="in", value=["Mining", "Renewables"])
    assert filt.value == ["Mining", "Renewables"]


def test_categorical_filter_value_not_observed_is_rejected():
    frame = pd.DataFrame({"sector": ["Mining", "Renewables", "Mining"]})
    spec = QuerySpec(board="deals", filters=[Filter(field="sector", op="eq", value="Aviation")], metric="count")
    with pytest.raises(QuerySpecError) as excinfo:
        validate_categorical_values(spec, frame)
    assert "Mining" in excinfo.value.hint  # valid values surfaced in the hint


def test_categorical_filter_value_observed_passes():
    frame = pd.DataFrame({"sector": ["Mining", "Renewables"]})
    spec = QuerySpec(board="deals", filters=[Filter(field="sector", op="eq", value="Mining")], metric="count")
    validate_categorical_values(spec, frame)  # must not raise


def test_numeric_filter_values_skip_categorical_validation():
    frame = pd.DataFrame({"deal_value": [1.0, 2.0]})
    spec = QuerySpec(
        board="deals", filters=[Filter(field="deal_value", op="eq", value=999.0)], metric="count"
    )
    validate_categorical_values(spec, frame)  # numeric eq is not a categorical check
