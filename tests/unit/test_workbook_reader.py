"""Tests for scripts/seeding/workbook.py — faithful xlsx reading (F03 section 3.9).

Run against the real source workbooks, not synthetic fixtures: the whole point
of this module is transporting the actual mess, so the actual mess is what
gets asserted on.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from scripts.seeding.errors import WorkbookError
from scripts.seeding.workbook import (
    WORK_ORDERS_SHEET_NAME,
    _read_sheet,
    read_deals_workbook,
    read_work_orders_workbook,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEALS_PATH = REPO_ROOT / "Deal funnel Data.xlsx"
WORK_ORDERS_PATH = REPO_ROOT / "Work_Order_Tracker Data.xlsx"


@pytest.fixture(scope="module")
def deals_result():
    return read_deals_workbook(DEALS_PATH)


@pytest.fixture(scope="module")
def work_orders_result():
    return read_work_orders_workbook(WORK_ORDERS_PATH)


def test_deals_headers_match_measured_shape(deals_result):
    assert deals_result.headers == [
        "Deal Name",
        "Owner code",
        "Client Code",
        "Deal Status",
        "Close Date (A)",
        "Closure Probability",
        "Masked Deal value",
        "Tentative Close Date",
        "Deal Stage",
        "Product deal",
        "Sector/service",
        "Created Date",
    ]


def test_work_orders_header_is_read_from_row_two(work_orders_result):
    assert len(work_orders_result.headers) == 38
    assert work_orders_result.headers[0] == "Deal name masked"
    assert work_orders_result.headers[2] == "Serial #"


def test_reading_work_orders_row_one_as_header_fails_loudly():
    """Row 1 of `work order tracker` is blank; reading it must raise, not
    silently yield 38 all-None columns."""
    with pytest.raises(WorkbookError):
        _read_sheet(WORK_ORDERS_PATH, WORK_ORDERS_SHEET_NAME, 1)


def test_deals_row_count_is_346(deals_result):
    """344 real deals + 2 embedded junk header rows, per CLAUDE.md."""
    assert deals_result.row_count == 346


def test_work_orders_row_count_is_176(work_orders_result):
    assert work_orders_result.row_count == 176


def test_embedded_junk_rows_are_kept_and_flagged(deals_result):
    assert set(deals_result.junk_rows) == {52, 181}
    by_row = {row.source_row: row for row in deals_result.rows}

    nezuko = by_row[52]
    assert nezuko.is_junk
    assert nezuko.values["Deal Name"] == "Nezuko"
    assert nezuko.values["Deal Status"] == "Deal Status"
    assert nezuko.values["Sector/service"] == "Sector/service"

    bugs_bunny = by_row[181]
    assert bugs_bunny.is_junk
    assert bugs_bunny.values["Deal Name"] == "Bugs Bunny"


def test_non_junk_rows_are_not_flagged(deals_result):
    non_junk = [row for row in deals_result.rows if not row.is_junk]
    assert len(non_junk) == 344
    assert all(row.values.get("Deal Status") != "Deal Status" for row in non_junk)


def test_native_types_survive_without_pandas_style_coercion(deals_result):
    non_junk = [row for row in deals_result.rows if not row.is_junk]
    assert any(
        isinstance(row.values.get("Created Date"), datetime.datetime)
        for row in non_junk
    )
    assert any(
        isinstance(row.values.get("Masked Deal value"), (int, float))
        for row in non_junk
    )
    assert any(row.values.get("Masked Deal value") == "" for row in non_junk)
    assert any(row.values.get("Close Date (A)") == "" for row in non_junk)


def test_value_error_literal_survives_verbatim(work_orders_result):
    """One `#VALUE!` Excel error exists; it must arrive as the literal string."""
    matches = [
        row
        for row in work_orders_result.rows
        if row.values.get("Amount in Rupees (Excl of GST) (Masked)") == "#VALUE!"
    ]
    assert len(matches) == 1


def test_serial_number_uniqueness_in_the_source(work_orders_result):
    serials = [row.values["Serial #"] for row in work_orders_result.rows]
    assert len(serials) == 176
    assert len(set(serials)) == 176


def test_unknown_workbook_path_raises_workbook_error(tmp_path):
    with pytest.raises(WorkbookError):
        read_deals_workbook(tmp_path / "does-not-exist.xlsx")


def test_unknown_sheet_name_raises_workbook_error(tmp_path):
    import openpyxl

    path = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(path)
    with pytest.raises(WorkbookError):
        _read_sheet(path, "no such sheet", 1)
