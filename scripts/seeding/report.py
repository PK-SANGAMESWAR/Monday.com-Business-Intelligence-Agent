"""SeedingReport: counts, unrepresentable values, verification, Markdown output.

Observability (NFR-8 / F03 section 3.7): a seeding run that does not say what
it did and what it could not represent is not trustworthy, per CLAUDE.md's
"surface caveats" rule applied to the seeder itself rather than the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "BoardCounts",
    "SeedingReport",
    "UnrepresentableValue",
    "VerificationCheck",
]


@dataclass(frozen=True)
class BoardCounts:
    board_name: str
    board_id: str | None
    total_rows: int
    created: int
    skipped_existing: int
    junk_rows: list[int] = field(default_factory=list)
    dropped_empty_rows: list[int] = field(default_factory=list)
    unnamed_items: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class UnrepresentableValue:
    board: str
    source_row: int
    header: str
    raw_value: Any


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class SeedingReport:
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    boards: list[BoardCounts] = field(default_factory=list)
    unrepresentable: list[UnrepresentableValue] = field(default_factory=list)
    verification: list[VerificationCheck] = field(default_factory=list)

    @property
    def verification_passed(self) -> bool:
        return all(check.passed for check in self.verification)

    def add_board(self, counts: BoardCounts) -> None:
        self.boards.append(counts)

    def add_unrepresentable(self, item: UnrepresentableValue) -> None:
        self.unrepresentable.append(item)

    def add_verification(self, check: VerificationCheck) -> None:
        self.verification.append(check)

    def to_markdown(self) -> str:
        lines: list[str] = [
            "# Seeding Report",
            "",
            f"Generated: {self.generated_at.isoformat()}",
            "",
            "## Boards",
            "",
        ]
        for counts in self.boards:
            lines.append(
                f"- **{counts.board_name}** ({counts.board_id or 'not yet created'}): "
                f"{counts.created} created, {counts.skipped_existing} already present, "
                f"{counts.total_rows} rows in the workbook"
            )
            if counts.junk_rows:
                lines.append(
                    f"  - junk header rows seeded (flagged, per CLAUDE.md): "
                    f"{counts.junk_rows}"
                )
            if counts.dropped_empty_rows:
                lines.append(
                    f"  - fully-empty rows dropped: {counts.dropped_empty_rows}"
                )
            if counts.unnamed_items:
                lines.append(
                    f"  - blank item names, written as '(unnamed deal)': "
                    f"{counts.unnamed_items}"
                )

        lines += ["", f"## Unrepresentable values ({len(self.unrepresentable)})", ""]
        if not self.unrepresentable:
            lines.append("None.")
        for item in self.unrepresentable:
            lines.append(
                f"- {item.board} row {item.source_row}, {item.header!r}: "
                f"{item.raw_value!r}"
            )

        lines += ["", "## Verification", ""]
        if not self.verification:
            lines.append("Not run.")
        for check in self.verification:
            mark = "PASS" if check.passed else "FAIL"
            lines.append(f"- [{mark}] {check.name}: {check.detail}")

        lines.append("")
        return "\n".join(lines)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(), encoding="utf-8")
        return path
