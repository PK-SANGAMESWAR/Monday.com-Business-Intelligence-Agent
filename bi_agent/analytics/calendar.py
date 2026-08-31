"""Indian fiscal year period resolution, anchored to the real clock (plan section 9.1 OQ-4).

FY = April-March, Q1 Apr-Jun ... Q4 Jan-Mar. `resolve_period` turns a phrase like
"this quarter" into a concrete date range; when that range has no rows, the *caller*
(`metrics.py`, which has the data) falls back to `most_recent_period_with_data` and says
so explicitly — this module never silently substitutes a different window on its own,
since it has no way to know whether one is needed.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd

__all__ = [
    "Period",
    "fiscal_quarter",
    "fiscal_year_range",
    "fiscal_year_start",
    "most_recent_period_with_data",
    "quarter_range",
    "resolve_period",
]

#: quarter -> (start_month, end_month) within the fiscal year.
_QUARTER_MONTHS: dict[int, tuple[int, int]] = {1: (4, 6), 2: (7, 9), 3: (10, 12), 4: (1, 3)}

_FY_EXPLICIT_RE = re.compile(r"^fy\s*(\d{2})-(\d{2})(?:\s*q([1-4]))?$")


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end: date

    def contains(self, value: date | None) -> bool:
        return value is not None and self.start <= value <= self.end


def fiscal_year_start(d: date) -> int:
    """The calendar year the containing fiscal year *starts* in (Apr-Mar)."""
    return d.year if d.month >= 4 else d.year - 1


def fiscal_quarter(d: date) -> int:
    """1-4: Q1 Apr-Jun, Q2 Jul-Sep, Q3 Oct-Dec, Q4 Jan-Mar."""
    for quarter, (start_month, end_month) in _QUARTER_MONTHS.items():
        months = (
            range(start_month, end_month + 1)
            if start_month <= end_month
            else list(range(start_month, 13)) + list(range(1, end_month + 1))
        )
        if d.month in months:
            return quarter
    raise AssertionError("unreachable")  # every month maps to exactly one quarter


def _fy_label(fy_start_year: int) -> str:
    return f"FY{fy_start_year % 100:02d}-{(fy_start_year + 1) % 100:02d}"


def quarter_range(fy_start_year: int, quarter: int) -> Period:
    start_month, end_month = _QUARTER_MONTHS[quarter]
    year = fy_start_year + (1 if quarter == 4 else 0)
    start = date(year, start_month, 1)
    end = date(year, end_month, monthrange(year, end_month)[1])
    return Period(label=f"{_fy_label(fy_start_year)} Q{quarter}", start=start, end=end)


def fiscal_year_range(fy_start_year: int) -> Period:
    return Period(
        label=_fy_label(fy_start_year),
        start=date(fy_start_year, 4, 1),
        end=date(fy_start_year + 1, 3, 31),
    )


def resolve_period(text: str, now: date) -> Period:
    """Resolve a phrase against `now`. Raises `ValueError` (caught by the caller as a
    correctable `QuerySpecError`, per F01) for anything not understood."""
    normalized = text.strip().lower()
    fy_year = fiscal_year_start(now)
    quarter = fiscal_quarter(now)

    if normalized in ("this quarter", "current quarter"):
        return quarter_range(fy_year, quarter)

    if normalized == "last quarter":
        prev_quarter, prev_fy_year = quarter - 1, fy_year
        if prev_quarter == 0:
            prev_quarter, prev_fy_year = 4, fy_year - 1
        return quarter_range(prev_fy_year, prev_quarter)

    if normalized in ("this year", "this fiscal year", "current fiscal year"):
        return fiscal_year_range(fy_year)

    if normalized in ("last year", "last fiscal year"):
        return fiscal_year_range(fy_year - 1)

    match = _FY_EXPLICIT_RE.match(normalized)
    if match:
        start_yy, end_yy, q = match.groups()
        explicit_fy_year = 2000 + int(start_yy)
        if int(end_yy) != (explicit_fy_year + 1) % 100:
            raise ValueError(f"not a valid consecutive fiscal year: {text!r}")
        return quarter_range(explicit_fy_year, int(q)) if q else fiscal_year_range(explicit_fy_year)

    raise ValueError(f"cannot resolve period {text!r}")


def most_recent_period_with_data(
    dates: pd.Series, *, granularity: Literal["quarter", "year"] = "quarter"
) -> Period | None:
    """The most recent quarter/year that has at least one non-null date in `dates`.

    Returns `None` if `dates` has no non-null values at all — the caller then has
    nothing to fall back to and must say the field is entirely unpopulated instead.
    """
    valid = pd.to_datetime(dates.dropna(), errors="coerce").dropna()
    if valid.empty:
        return None
    latest = valid.max()
    latest_date = latest.date() if hasattr(latest, "date") else latest
    fy_year = fiscal_year_start(latest_date)
    if granularity == "quarter":
        return quarter_range(fy_year, fiscal_quarter(latest_date))
    return fiscal_year_range(fy_year)
