"""Tests for bi_agent/analytics/calendar.py — Indian fiscal year (Apr-Mar)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from bi_agent.analytics.calendar import (
    fiscal_quarter,
    fiscal_year_range,
    fiscal_year_start,
    most_recent_period_with_data,
    quarter_range,
    resolve_period,
)

#: The date this whole session is anchored to.
TODAY = date(2026, 8, 31)


@pytest.mark.parametrize(
    "day, expected_fy_start",
    [
        (date(2026, 4, 1), 2026),
        (date(2026, 3, 31), 2025),
        (date(2025, 12, 25), 2025),
    ],
)
def test_fiscal_year_start(day, expected_fy_start):
    assert fiscal_year_start(day) == expected_fy_start


@pytest.mark.parametrize(
    "day, expected_quarter",
    [
        (date(2026, 4, 1), 1),
        (date(2026, 6, 30), 1),
        (date(2026, 7, 1), 2),
        (date(2026, 9, 30), 2),
        (date(2026, 10, 1), 3),
        (date(2026, 12, 31), 3),
        (date(2026, 1, 1), 4),
        (date(2026, 3, 31), 4),
    ],
)
def test_fiscal_quarter(day, expected_quarter):
    assert fiscal_quarter(day) == expected_quarter


def test_quarter_range_q4_spans_the_calendar_year_boundary():
    period = quarter_range(2025, 4)  # FY25-26 Q4 = Jan-Mar 2026
    assert period.start == date(2026, 1, 1)
    assert period.end == date(2026, 3, 31)
    assert period.label == "FY25-26 Q4"


def test_fiscal_year_range():
    period = fiscal_year_range(2025)
    assert period.start == date(2025, 4, 1)
    assert period.end == date(2026, 3, 31)
    assert period.label == "FY25-26"


def test_this_quarter_resolves_against_now():
    """`now` = 2026-08-31 -> FY26-27 Q2 (Jul-Sep 2026)."""
    period = resolve_period("this quarter", TODAY)
    assert period.label == "FY26-27 Q2"
    assert period.start == date(2026, 7, 1)
    assert period.end == date(2026, 9, 30)


def test_last_quarter_resolves_to_the_prior_quarter():
    period = resolve_period("last quarter", TODAY)
    assert period.label == "FY26-27 Q1"
    assert period.start == date(2026, 4, 1)


def test_last_quarter_crosses_a_fiscal_year_boundary():
    period = resolve_period("last quarter", date(2026, 4, 15))  # now is FY26-27 Q1
    assert period.label == "FY25-26 Q4"
    assert period.start == date(2026, 1, 1)


def test_this_fiscal_year_and_last_fiscal_year():
    assert resolve_period("this fiscal year", TODAY).label == "FY26-27"
    assert resolve_period("last fiscal year", TODAY).label == "FY25-26"


def test_explicit_fy_with_quarter():
    period = resolve_period("FY25-26 Q3", TODAY)
    assert period.start == date(2025, 10, 1)
    assert period.end == date(2025, 12, 31)


def test_explicit_fy_without_quarter():
    period = resolve_period("FY25-26", TODAY)
    assert period.start == date(2025, 4, 1)
    assert period.end == date(2026, 3, 31)


def test_unrecognized_phrase_raises_value_error():
    with pytest.raises(ValueError):
        resolve_period("sometime next week", TODAY)


def test_non_consecutive_fy_years_rejected():
    with pytest.raises(ValueError):
        resolve_period("FY25-28", TODAY)


def test_most_recent_period_with_data():
    dates = pd.Series([None, "2024-08-09", "2026-01-09", None])
    period = most_recent_period_with_data(dates, granularity="quarter")
    assert period is not None
    assert period.label == "FY25-26 Q4"  # 2026-01-09 -> Jan is Q4 of FY25-26


def test_most_recent_period_with_data_all_null_returns_none():
    dates = pd.Series([None, None])
    assert most_recent_period_with_data(dates) is None
