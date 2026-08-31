"""`DataQualityReport`: coverage, always-null fields, conflicts, unrepresentable values.

Built once per fetch (`repository.py`) from a `NormalizedBoard` and its schema, and
exposed as its own agent tool in F06 (`data_quality_report`) rather than recomputed per
question — the report describes the *board*, not one query's slice of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from bi_agent.data.normalize import NormalizedBoard
from bi_agent.data.schema import FieldSpec

__all__ = ["DataQualityReport", "FieldCoverage", "build_quality_report"]


@dataclass(frozen=True)
class FieldCoverage:
    field: str
    n_total: int
    n_present: int
    n_unrepresentable: int
    always_null: bool

    @property
    def n_missing(self) -> int:
        return self.n_total - self.n_present - self.n_unrepresentable

    @property
    def coverage_ratio(self) -> float:
        return self.n_present / self.n_total if self.n_total else 0.0


@dataclass(frozen=True)
class DataQualityReport:
    board: str
    n_total_rows: int
    n_junk_rows_excluded: int
    coverage: dict[str, FieldCoverage]
    stage_status_conflicts: int
    casing_fixes: dict[str, int]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def always_null_fields(self) -> list[str]:
        return [name for name, cov in self.coverage.items() if cov.always_null]

    def sparse_fields(self, threshold: float = 0.2) -> list[str]:
        """Fields with less than `threshold` coverage, excluding the always-null ones
        (those get their own, stronger statement — see `always_null_fields`)."""
        return [
            name
            for name, cov in self.coverage.items()
            if not cov.always_null and cov.n_total and cov.coverage_ratio < threshold
        ]


def build_quality_report(
    normalized: NormalizedBoard, fields: tuple[FieldSpec, ...]
) -> DataQualityReport:
    frame = normalized.frame
    non_junk = frame.loc[~frame["is_junk"]]
    n_total = len(non_junk)

    coverage: dict[str, FieldCoverage] = {}
    for spec in fields:
        column = non_junk[spec.canonical] if spec.canonical in non_junk.columns else pd.Series(dtype=object)
        n_unrepresentable = normalized.unrepresentable.get(spec.canonical, 0)
        n_present = int(column.notna().sum())
        coverage[spec.canonical] = FieldCoverage(
            field=spec.canonical,
            n_total=n_total,
            n_present=n_present,
            n_unrepresentable=n_unrepresentable,
            always_null=spec.always_null,
        )

    stage_status_conflicts = (
        int((~frame.loc[~frame["is_junk"], "stage_status_consistent"]).sum())
        if "stage_status_consistent" in frame.columns
        else 0
    )

    return DataQualityReport(
        board=normalized.board,
        n_total_rows=n_total,
        n_junk_rows_excluded=normalized.n_junk_rows,
        coverage=coverage,
        stage_status_conflicts=stage_status_conflicts,
        casing_fixes=dict(normalized.casing_fixes),
    )
