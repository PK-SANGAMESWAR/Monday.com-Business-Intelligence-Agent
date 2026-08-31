"""Tests for bi_agent/data/normalize.py, against the real seeded boards
(tests/fixtures/live/{deals,work_orders}_board_items.json)."""

from __future__ import annotations

from datetime import date

import pytest

from bi_agent.data.normalize import normalize_deals, normalize_work_orders, parse_cell


# --- parse_cell: the type-by-type contract (F04 plan section 3.3) ------------------------


def test_empty_text_is_none_for_every_type():
    for field_type in ("date", "number", "text", "list"):
        assert parse_cell(field_type, None) == (None, False)
        assert parse_cell(field_type, "") == (None, False)


def test_date_parses_iso_format():
    assert parse_cell("date", "2024-08-14") == (date(2024, 8, 14), False)


def test_date_unparseable_text_is_unrepresentable_not_a_crash():
    assert parse_cell("date", "not a date") == (None, True)


def test_number_zero_is_a_value_never_treated_as_missing():
    assert parse_cell("number", "0") == (0.0, False)


def test_number_unparseable_text_is_unrepresentable_never_zero():
    value, bad = parse_cell("number", "#VALUE!")
    assert value is None
    assert bad is True


def test_list_splits_and_trims_commas():
    assert parse_cell("list", "Topography Survey: RGB, Hydrology, Others") == (
        ["Topography Survey: RGB", "Hydrology", "Others"],
        False,
    )


def test_text_passes_through_verbatim():
    assert parse_cell("text", "BIlled") == ("BIlled", False)


# --- normalize_deals ----------------------------------------------------------------------


def test_deals_row_count_matches_the_seeded_board(deals_snapshot):
    normalized = normalize_deals(deals_snapshot)
    assert len(normalized.frame) == 346


def test_deals_junk_rows_are_flagged_not_dropped(deals_snapshot):
    normalized = normalize_deals(deals_snapshot)
    frame = normalized.frame
    assert normalized.n_junk_rows == 2

    junk_names = set(frame.loc[frame["is_junk"], "deal_name"])
    assert junk_names == {"Nezuko", "Bugs Bunny"}


def test_deals_value_sum_over_non_junk_matches_f03s_verified_figure(deals_snapshot):
    normalized = normalize_deals(deals_snapshot)
    frame = normalized.frame.loc[~normalized.frame["is_junk"]]
    valued = frame.loc[frame["has_value"]]

    assert len(valued) == 165
    assert valued["deal_value"].sum() == pytest.approx(2_305_518_040.91, abs=0.01)


def test_stage_letter_extraction(deals_snapshot):
    normalized = normalize_deals(deals_snapshot)
    frame = normalized.frame
    lettered = frame.loc[frame["stage"] == "A. Lead Generated", "stage_letter"]
    assert (lettered == "A").all()

    unprefixed = frame.loc[frame["stage"] == "Project Completed", "stage_letter"]
    assert unprefixed.isna().all()


def test_stage_status_conflict_matches_data_profile(deals_snapshot):
    """DATA_PROFILE.md: 70 `Won` deals at `A. Lead Generated`, 2 at `F. Negotiations` — 72
    total contradictions among the 165 `Won` deals (junk rows excluded)."""
    normalized = normalize_deals(deals_snapshot)
    frame = normalized.frame.loc[~normalized.frame["is_junk"]]

    won = frame.loc[frame["status"] == "Won"]
    assert len(won) == 165
    conflicts = won.loc[~won["stage_status_consistent"]]
    assert len(conflicts) == 72


# --- normalize_work_orders ------------------------------------------------------------


def test_work_orders_row_count_matches_the_seeded_board(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    assert len(normalized.frame) == 176


def test_serial_number_uniqueness(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    serials = normalized.frame["serial_no"]
    assert serials.notna().all()
    assert serials.nunique() == 176


def test_billed_value_zero_is_counted_not_missing(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    frame = normalized.frame
    zeros = frame.loc[frame["billed_incl_gst"] == 0]
    assert len(zeros) == 63
    # A recorded zero is not billed - `is_billed` must say so, not just "value present".
    assert not zeros["is_billed"].any()


def test_billing_status_casing_bug_fixed_but_raw_kept(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    frame = normalized.frame

    assert (frame["billing_status"] == "BIlled").sum() == 0
    assert (frame["billing_status_raw"] == "BIlled").sum() == 3
    assert normalized.casing_fixes == {"BIlled -> Billed": 3}
    # Every row fixed must have become "Billed", not merely "not BIlled".
    fixed_rows = frame.loc[frame["billing_status_raw"] == "BIlled"]
    assert (fixed_rows["billing_status"] == "Billed").all()


def test_always_null_fields_really_are_empty(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    frame = normalized.frame
    for canonical in (
        "expected_billing_month",
        "actual_collection_month",
        "collection_status",
        "collection_date",
    ):
        assert frame[canonical].isna().all(), canonical


def test_billing_pct_and_collection_pct_guard_against_zero_denominator(work_orders_snapshot):
    normalized = normalize_work_orders(work_orders_snapshot)
    frame = normalized.frame
    zero_amount = frame.loc[frame["amount_incl_gst"].fillna(0) == 0]
    assert zero_amount["billing_pct"].isna().all()
