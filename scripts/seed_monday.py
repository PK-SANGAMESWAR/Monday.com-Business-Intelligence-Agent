"""Seed the Deals and Work Orders boards on monday.com from the two workbooks.

The only component in this repository that writes to monday.com (F03). It is
not imported by `bi_agent/` and a test (`tests/unit/test_write_gate.py`)
asserts that.

Usage::

    uv run python scripts/seed_monday.py --dry-run
    uv run python scripts/seed_monday.py
    uv run python scripts/seed_monday.py --only deals
    uv run python scripts/seed_monday.py --recreate --yes
    uv run python scripts/seed_monday.py --verify-only

Per CLAUDE.md / F03 section 3.5, the workbook mess (embedded junk header
rows, `#VALUE!`, mixed-unit quantities, the unprefixed `Project Completed`
stage) is transported verbatim, not cleaned — F04 cleans, this script only
transports.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bi_agent.config import load_settings  # noqa: E402
from bi_agent.errors import BIAgentError, MondayQueryError, SchemaMismatchError  # noqa: E402
from bi_agent.logging_config import configure_logging  # noqa: E402
from bi_agent.monday.boards import BoardReader, BoardSnapshot  # noqa: E402
from bi_agent.monday.client import MondayClient  # noqa: E402
from scripts.seeding.errors import SeedError  # noqa: E402
from scripts.seeding.mutations import (  # noqa: E402
    CREATE_BOARD,
    CREATE_COLUMN,
    CREATE_ITEM,
    DELETE_BOARD,
    DELETE_COLUMN,
    DELETE_ITEM,
)
from scripts.seeding.report import (  # noqa: E402
    BoardCounts,
    SeedingReport,
    UnrepresentableValue,
    VerificationCheck,
)
from scripts.seeding.schema import (  # noqa: E402
    MONDAY_COLUMN_TYPE,
    DEALS_COLUMNS,
    DEALS_ITEM_NAME_HEADER,
    SOURCE_ROW_COLUMN_TYPE,
    SOURCE_ROW_HEADER,
    WORK_ORDERS_COLUMNS,
    WORK_ORDERS_ITEM_NAME_HEADER,
    ColumnSpec,
    encode_source_row,
    encode_value,
)
from scripts.seeding.workbook import (  # noqa: E402
    DEALS_WORKBOOK_FILENAME,
    WORK_ORDERS_WORKBOOK_FILENAME,
    WorkbookReadResult,
    WorkbookRow,
    read_deals_workbook,
    read_work_orders_workbook,
)
from scripts.seeding.writer import Pacer, SeedWriter, encode_column_values  # noqa: E402

__all__ = ["DEALS_TARGET", "SeedTarget", "WORK_ORDERS_TARGET", "main"]

#: Conservative default absent a calibration probe (F03 section 3.7): write
#: mutations cost materially more than reads, but even a pessimistic
#: per-item cost leaves this far under any plan's per-minute complexity
#: budget. Override with --items-per-minute once a probe has measured the
#: real number. Lowered from an initial 30 after the first live run showed a
#: sustained request-rate throttle (HTTP 429 with an HTML, non-GraphQL body -
#: a WAF/CDN limit, not the GraphQL complexity budget) engaging after roughly
#: 70 requests at that pace; see F03 section 9.
DEFAULT_ITEMS_PER_MINUTE = 20.0

#: The write path retries harder than the default `Settings.max_retries`
#: (3): the observed throttle window outlasts three retries' worth of
#: exponential backoff even with the 30s cap, so a plain resume run would
#: give up mid-throttle instead of riding it out. Reads keep the default -
#: they are infrequent here (one board resolution, one items fetch per
#: board) and are not what trips the limit.
WRITER_MAX_RETRIES = 8


class SeedTarget:
    """Everything that differs between the Deals and Work Orders boards."""

    def __init__(
        self,
        *,
        key: str,
        board_name: str,
        workbook_filename: str,
        read_workbook: Callable[[Path], WorkbookReadResult],
        columns: tuple[ColumnSpec, ...],
        item_name_header: str,
        source_row_prefix: str,
        unnamed_item_label: str,
    ) -> None:
        self.key = key
        self.board_name = board_name
        self.workbook_filename = workbook_filename
        self.read_workbook = read_workbook
        self.columns = columns
        self.item_name_header = item_name_header
        self.source_row_prefix = source_row_prefix
        self.unnamed_item_label = unnamed_item_label

    @property
    def expected_titles(self) -> list[str]:
        return [spec.header for spec in self.columns] + [SOURCE_ROW_HEADER]


DEALS_TARGET = SeedTarget(
    key="deals",
    board_name="Deals",
    workbook_filename=DEALS_WORKBOOK_FILENAME,
    read_workbook=read_deals_workbook,
    columns=DEALS_COLUMNS,
    item_name_header=DEALS_ITEM_NAME_HEADER,
    source_row_prefix="DEAL",
    unnamed_item_label="(unnamed deal)",
)

WORK_ORDERS_TARGET = SeedTarget(
    key="work-orders",
    board_name="Work Orders",
    workbook_filename=WORK_ORDERS_WORKBOOK_FILENAME,
    read_workbook=read_work_orders_workbook,
    columns=WORK_ORDERS_COLUMNS,
    item_name_header=WORK_ORDERS_ITEM_NAME_HEADER,
    source_row_prefix="WO",
    unnamed_item_label="(unnamed work order)",
)

TARGETS: tuple[SeedTarget, ...] = (DEALS_TARGET, WORK_ORDERS_TARGET)


# --- row -> payload -----------------------------------------------------------


def build_row_payload(
    target: SeedTarget, row: WorkbookRow
) -> tuple[str, dict[str, Any], list[tuple[str, Any]]]:
    """One row -> (item name, {header: encoded value}, [(header, raw) unrepresentable])."""
    raw_name = row.values.get(target.item_name_header)
    if isinstance(raw_name, str) and raw_name.strip():
        item_name = raw_name.strip()
    elif raw_name not in (None, ""):
        item_name = str(raw_name)
    else:
        item_name = target.unnamed_item_label

    values: dict[str, Any] = {}
    unrepresentable: list[tuple[str, Any]] = []
    for spec in target.columns:
        result = encode_value(spec.column_type, row.values.get(spec.header))
        if result.kind == "value":
            values[spec.header] = result.value
        elif result.kind == "unrepresentable":
            unrepresentable.append((spec.header, result.raw))

    values[SOURCE_ROW_HEADER] = encode_source_row(target.source_row_prefix, row.source_row)
    return item_name, values, unrepresentable


# --- board resolution: create, resume, or recreate -----------------------------


def _existing_source_rows(snapshot: BoardSnapshot) -> set[str]:
    source_row_id = snapshot.column_id(SOURCE_ROW_HEADER)
    if source_row_id is None:
        return set()
    found: set[str] = set()
    for item in snapshot.items:
        for column_value in item.get("column_values") or []:
            if column_value.get("id") == source_row_id:
                text = column_value.get("text")
                if text:
                    found.add(str(text))
    return found


def resolve_or_create_board(
    target: SeedTarget,
    *,
    reader: BoardReader,
    writer: SeedWriter,
    pacer: Pacer,
    recreate: bool,
    confirm: Callable[[str], bool],
) -> tuple[str, dict[str, str], set[str]]:
    """Returns (board_id, {title: column_id}, existing Source Row values).

    Every write here is paced too, not only `create_item` in `seed_board`: an
    unthrottled burst of `create_column`/`delete_column` calls during board
    setup is exactly what tripped monday.com's request-rate limit on the
    first live run (F03 section 9) before a single item was ever written.
    """
    try:
        ref = reader.resolve_board(target.board_name)
        exists = True
    except MondayQueryError:
        ref = None
        exists = False

    if exists and recreate:
        probe = reader.fetch_items(target.board_name, force_refresh=True)
        if not confirm(
            f"--recreate will DELETE board {target.board_name!r} "
            f"(id {probe.board_id}, {probe.item_count} items) and rebuild it "
            f"from the workbook. Type {target.board_name!r} to confirm: "
        ):
            raise SeedError(
                f"--recreate for {target.board_name!r} was not confirmed; "
                "nothing was deleted."
            )
        pacer.wait()
        writer.execute(DELETE_BOARD, {"boardId": probe.board_id})
        reader.invalidate(probe.board_id)
        exists = False

    if exists and ref is not None:
        snapshot = reader.fetch_items(target.board_name, force_refresh=True)
        missing = [t for t in target.expected_titles if t not in snapshot.column_index]
        if missing:
            raise SeedError(
                f"board {target.board_name!r} ({snapshot.board_id}) exists but "
                f"is missing expected column(s) {missing}. Refusing to write "
                "into a board whose schema does not match — this is very "
                "likely somebody else's board of the same name."
            )
        return (
            snapshot.board_id,
            dict(snapshot.column_index),
            _existing_source_rows(snapshot),
        )

    pacer.wait()
    created = writer.execute(CREATE_BOARD, {"name": target.board_name, "kind": "public"})
    board_id = str(created["create_board"]["id"])
    reader.invalidate()

    for column in reader.fetch_columns(board_id):
        # The implicit "name" column is listed alongside the real default
        # columns but is mandatory and cannot be deleted (monday.com rejects
        # it with DeleteMandatoryColumnException) — it is not one of the
        # `person`/`status`/`date4`/`subitems` defaults section 3.3 means.
        if column.id == "name":
            continue
        pacer.wait()
        writer.execute(DELETE_COLUMN, {"boardId": board_id, "columnId": column.id})

    column_index: dict[str, str] = {}
    all_specs = (*target.columns, ColumnSpec(SOURCE_ROW_HEADER, SOURCE_ROW_COLUMN_TYPE))
    for spec in all_specs:
        pacer.wait()
        result = writer.execute(
            CREATE_COLUMN,
            {
                "boardId": board_id,
                "title": spec.header,
                "columnType": MONDAY_COLUMN_TYPE[spec.column_type],
            },
        )
        column_index[spec.header] = str(result["create_column"]["id"])

    # A brand-new board carries one auto-generated sample item ("Task 1",
    # observed on the first live run) that the `columns` query used above
    # never reveals. Nothing of ours exists yet, so every item found here is
    # that stray default — delete it before any workbook row is written, or
    # it silently inflates every item count downstream.
    reader.invalidate(board_id)
    for item in reader.fetch_items(board_id, force_refresh=True).items:
        pacer.wait()
        writer.execute(DELETE_ITEM, {"itemId": item["id"]})
    reader.invalidate(board_id)

    return board_id, column_index, set()


# --- seeding one board ----------------------------------------------------


def seed_board(
    target: SeedTarget,
    result: WorkbookReadResult,
    *,
    reader: BoardReader,
    writer: SeedWriter,
    pacer: Pacer,
    recreate: bool,
    confirm: Callable[[str], bool],
    report: SeedingReport,
    progress: Callable[[str], None] = lambda message: None,
) -> str:
    board_id, column_index, existing = resolve_or_create_board(
        target, reader=reader, writer=writer, pacer=pacer, recreate=recreate, confirm=confirm
    )

    created = 0
    skipped = 0
    unnamed_rows: list[int] = []

    for row in result.rows:
        item_name, values, unrepresentable = build_row_payload(target, row)
        if item_name == target.unnamed_item_label:
            unnamed_rows.append(row.source_row)
        for header, raw in unrepresentable:
            report.add_unrepresentable(
                UnrepresentableValue(target.board_name, row.source_row, header, raw)
            )

        source_row_value = values[SOURCE_ROW_HEADER]
        if source_row_value in existing:
            skipped += 1
            continue

        column_values_by_id = {
            column_index[header]: value
            for header, value in values.items()
            if header in column_index
        }
        pacer.wait()
        writer.execute(
            CREATE_ITEM,
            {
                "boardId": board_id,
                "itemName": item_name,
                "columnValues": encode_column_values(column_values_by_id),
            },
        )
        created += 1
        if created % 25 == 0:
            progress(f"{target.board_name}: {created} created, {skipped} skipped so far")

    report.add_board(
        BoardCounts(
            board_name=target.board_name,
            board_id=board_id,
            total_rows=result.row_count,
            created=created,
            skipped_existing=skipped,
            junk_rows=result.junk_rows,
            dropped_empty_rows=result.dropped_empty_rows,
            unnamed_items=unnamed_rows,
        )
    )
    return board_id


# --- verification (F03 section 3.8) --------------------------------------


def _sum_numbers_column(snapshot: BoardSnapshot, column_id: str) -> tuple[float, int]:
    total, n = 0.0, 0
    for item in snapshot.items:
        for column_value in item.get("column_values") or []:
            if column_value.get("id") != column_id:
                continue
            text = column_value.get("text")
            if text in (None, ""):
                continue
            try:
                total += float(text)
                n += 1
            except ValueError:
                pass
    return total, n


def _sum_numbers_workbook(result: WorkbookReadResult, header: str) -> tuple[float, int]:
    total, n = 0.0, 0
    for row in result.rows:
        encoded = encode_value("numbers", row.values.get(header))
        if encoded.kind == "value":
            total += float(encoded.value)
            n += 1
    return total, n


def verify_board(
    target: SeedTarget,
    result: WorkbookReadResult,
    board_id: str,
    *,
    reader: BoardReader,
    report: SeedingReport,
) -> None:
    """Read the board back through F02 and check it against the workbook."""
    snapshot = reader.fetch_items(board_id, force_refresh=True)

    report.add_verification(
        VerificationCheck(
            name=f"{target.board_name} item count",
            passed=snapshot.item_count == result.row_count,
            detail=f"expected {result.row_count}, board has {snapshot.item_count}",
        )
    )

    try:
        snapshot.require_columns(target.expected_titles)
        report.add_verification(
            VerificationCheck(
                name=f"{target.board_name} columns present",
                passed=True,
                detail=f"all {len(target.expected_titles)} expected columns resolved",
            )
        )
    except SchemaMismatchError as exc:
        report.add_verification(
            VerificationCheck(
                name=f"{target.board_name} columns present", passed=False, detail=str(exc)
            )
        )

    for spec in target.columns:
        if spec.column_type != "numbers":
            continue
        column_id = snapshot.column_id(spec.header)
        if column_id is None:
            continue
        board_sum, board_n = _sum_numbers_column(snapshot, column_id)
        workbook_sum, workbook_n = _sum_numbers_workbook(result, spec.header)
        passed = board_n == workbook_n and abs(board_sum - workbook_sum) < 0.01
        report.add_verification(
            VerificationCheck(
                name=f"{target.board_name} {spec.header!r} sum round-trip",
                passed=passed,
                detail=(
                    f"workbook: {workbook_sum} over {workbook_n} row(s); "
                    f"board: {board_sum} over {board_n} row(s)"
                ),
            )
        )

    if target is WORK_ORDERS_TARGET:
        names = [item.get("name") for item in snapshot.items]
        unique = len(set(names))
        report.add_verification(
            VerificationCheck(
                name="Serial # uniqueness",
                passed=unique == len(names),
                detail=f"{unique} unique of {len(names)} items",
            )
        )


# --- .env -------------------------------------------------------------------


def write_env_board_id(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found = False
    updated: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{key}={value}")
    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")


# --- dry run ----------------------------------------------------------------


def print_dry_run(
    targets: tuple[SeedTarget, ...],
    results: dict[str, WorkbookReadResult],
    items_per_minute: float,
    *,
    out: Callable[[str], None] = print,
) -> None:
    total_rows = 0
    for target in targets:
        result = results[target.key]
        out(f"\n{target.board_name} ({target.workbook_filename}):")
        out(f"  rows to create: {result.row_count}")
        if result.junk_rows:
            out(f"  junk header rows (seeded, flagged per CLAUDE.md): {result.junk_rows}")
        if result.dropped_empty_rows:
            out(f"  fully-empty rows dropped: {result.dropped_empty_rows}")
        out(f"  columns: {len(target.columns) + 1} (including {SOURCE_ROW_HEADER!r})")
        for spec in target.columns:
            out(f"    - {spec.header!r} -> {spec.column_type}")
        out(f"    - {SOURCE_ROW_HEADER!r} -> {SOURCE_ROW_COLUMN_TYPE} (provenance, not in source)")
        total_rows += result.row_count

    minutes = total_rows / items_per_minute if items_per_minute else 0.0
    out(f"\nTotal items to create: {total_rows}")
    out(f"At {items_per_minute:g}/minute: ~{minutes:.1f} minute(s)")
    out("\nDRY RUN: zero requests were sent.")


# --- CLI ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="plan only, send nothing")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="delete and rebuild an existing board of the target name (destructive; confirmed)",
    )
    parser.add_argument("--only", choices=("deals", "work-orders"), default=None)
    parser.add_argument("--items-per-minute", type=float, default=DEFAULT_ITEMS_PER_MINUTE)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="skip writing; only run the section-3.8 verification against existing boards",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="answer the --recreate confirmation prompt affirmatively (non-interactive)",
    )
    args = parser.parse_args(argv)

    targets = tuple(t for t in TARGETS if args.only is None or t.key == args.only)

    results: dict[str, WorkbookReadResult] = {}
    for target in targets:
        try:
            results[target.key] = target.read_workbook(REPO_ROOT / target.workbook_filename)
        except BIAgentError as exc:
            print(f"error reading {target.workbook_filename}: {exc}", file=sys.stderr)
            return 2

    if args.dry_run:
        print_dry_run(targets, results, args.items_per_minute)
        return 0

    try:
        settings = load_settings()
    except BIAgentError as exc:
        print(f"error: {exc.user_message}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level, secrets=settings.secret_values())

    def confirm(prompt: str) -> bool:
        if args.yes:
            return True
        try:
            return input(prompt).strip() != ""
        except EOFError:
            return False

    report = SeedingReport()
    exit_code = 0

    writer_settings = settings.model_copy(update={"max_retries": WRITER_MAX_RETRIES})

    with MondayClient(settings) as read_client, SeedWriter(writer_settings) as writer:
        reader = BoardReader(read_client)
        pacer = Pacer(args.items_per_minute)

        for target in targets:
            result = results[target.key]
            print(f"\n=== {target.board_name} ===")
            try:
                if args.verify_only:
                    board_id = reader.resolve_board(target.board_name).id
                else:
                    board_id = seed_board(
                        target,
                        result,
                        reader=reader,
                        writer=writer,
                        pacer=pacer,
                        recreate=args.recreate,
                        confirm=confirm,
                        report=report,
                        progress=print,
                    )
                verify_board(target, result, board_id, reader=reader, report=report)
            except BIAgentError as exc:
                print(f"error seeding {target.board_name}: {exc}", file=sys.stderr)
                exit_code = 1
                continue

            if not args.verify_only:
                env_key = (
                    "MONDAY_DEALS_BOARD_ID"
                    if target.key == "deals"
                    else "MONDAY_WORK_ORDERS_BOARD_ID"
                )
                write_env_board_id(REPO_ROOT / ".env", env_key, board_id)

    report_path = report.write(REPO_ROOT / "docs" / "SEEDING_REPORT.md")
    print(f"\nWrote {report_path.relative_to(REPO_ROOT)}")
    for check in report.verification:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name}: {check.detail}")

    if not report.verification_passed:
        print("\nVerification FAILED - see the report above.", file=sys.stderr)
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
