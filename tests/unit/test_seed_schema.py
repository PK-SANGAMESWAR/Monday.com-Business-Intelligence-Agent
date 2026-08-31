"""Tests for scripts/seeding/schema.py — header -> type -> encoder (F03 section 3.4)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

from scripts.seeding.schema import (
    DEALS_COLUMNS,
    DEALS_ITEM_NAME_HEADER,
    WORK_ORDERS_COLUMNS,
    WORK_ORDERS_ITEM_NAME_HEADER,
    encode_source_row,
    encode_value,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_deals_header_is_mapped_or_is_the_item_name():
    workbook = openpyxl.load_workbook(REPO_ROOT / "Deal funnel Data.xlsx", data_only=True)
    headers = {cell.value for cell in workbook["Deal tracker"][1]}
    mapped = {spec.header for spec in DEALS_COLUMNS} | {DEALS_ITEM_NAME_HEADER}
    assert headers == mapped
    assert len(DEALS_COLUMNS) == 11


def test_every_work_orders_header_is_mapped_or_is_the_item_name():
    workbook = openpyxl.load_workbook(
        REPO_ROOT / "Work_Order_Tracker Data.xlsx", data_only=True
    )
    headers = {cell.value for cell in workbook["work order tracker"][2]}
    mapped = {spec.header for spec in WORK_ORDERS_COLUMNS} | {
        WORK_ORDERS_ITEM_NAME_HEADER
    }
    assert headers == mapped
    assert len(WORK_ORDERS_COLUMNS) == 37


def test_no_header_is_mapped_twice_within_a_board():
    deals_headers = [spec.header for spec in DEALS_COLUMNS]
    work_orders_headers = [spec.header for spec in WORK_ORDERS_COLUMNS]
    assert len(deals_headers) == len(set(deals_headers))
    assert len(work_orders_headers) == len(set(work_orders_headers))


@pytest.mark.parametrize(
    "raw, expected",
    [
        (datetime(2024, 8, 14), {"date": "2024-08-14"}),
        (date(2024, 8, 14), {"date": "2024-08-14"}),
    ],
)
def test_date_encoding(raw, expected):
    result = encode_value("date", raw)
    assert result.kind == "value"
    assert result.value == expected


def test_date_empty_is_omitted_not_reported():
    assert encode_value("date", None).kind == "empty"
    assert encode_value("date", "").kind == "empty"
    assert encode_value("date", "   ").kind == "empty"


def test_date_holding_text_is_unrepresentable_not_a_crash():
    result = encode_value("date", "NA verbal confirmation for km")
    assert result.kind == "unrepresentable"
    assert result.raw == "NA verbal confirmation for km"
    assert result.value is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        (1250000, "1250000"),
        (1250000.5, "1250000.5"),
        (751473450, "751473450"),
        (51440, "51440"),
    ],
)
def test_number_encoding(raw, expected):
    result = encode_value("numbers", raw)
    assert result.kind == "value"
    assert result.value == expected


def test_zero_encodes_as_zero_never_omitted():
    """63 work orders have a real, recorded zero — CLAUDE.md's "zero as missing" bug."""
    result = encode_value("numbers", 0)
    assert result.kind == "value"
    assert result.value == "0"


def test_number_empty_string_and_none_are_omitted():
    assert encode_value("numbers", None).kind == "empty"
    assert encode_value("numbers", "").kind == "empty"
    assert encode_value("numbers", "   ").kind == "empty"


def test_value_error_in_numbers_column_is_unrepresentable_never_zero():
    result = encode_value("numbers", "#VALUE!")
    assert result.kind == "unrepresentable"
    assert result.raw == "#VALUE!"
    assert result.value is None


def test_free_text_passes_through_verbatim():
    for raw in (
        "NA verbal confirmation for km",
        "5360 HA",
        "BIlled",
        "Project Completed",
        "Billed- Visit 7",
    ):
        result = encode_value("text", raw)
        assert result.kind == "value"
        assert result.value == raw


def test_text_empty_is_omitted():
    assert encode_value("text", None).kind == "empty"
    assert encode_value("text", "").kind == "empty"


def test_source_row_encoding_is_zero_padded_so_it_sorts():
    assert encode_source_row("DEAL", 52) == "DEAL-0052"
    assert encode_source_row("WO", 113) == "WO-0113"
    assert encode_source_row("DEAL", 5) == "DEAL-0005"
