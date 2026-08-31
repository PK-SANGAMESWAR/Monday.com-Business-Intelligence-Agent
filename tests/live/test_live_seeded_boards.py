"""F03 section 3.8: verify the real, seeded Deals and Work Orders boards.

Deselected by default; run with `-m live` **after** `scripts/seed_monday.py`
has actually seeded both boards (`Settings.boards_configured` must be true).
This is the "live verification of F02" half of F03 — the F02 test suite is
re-run unchanged elsewhere; this file checks the seeded data itself.

Run:  uv run pytest -m live -v tests/live/test_live_seeded_boards.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bi_agent.monday.boards import BoardReader
from bi_agent.monday.client import MondayClient
from scripts.seed_monday import DEALS_TARGET, WORK_ORDERS_TARGET, verify_board
from scripts.seeding.report import SeedingReport
from scripts.seeding.workbook import read_deals_workbook, read_work_orders_workbook

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def live_client(live_settings):
    with MondayClient(live_settings) as client:
        yield client


@pytest.fixture(autouse=True)
def _require_seeded_boards(live_settings):
    if not live_settings.boards_configured:
        pytest.skip(
            "MONDAY_DEALS_BOARD_ID / MONDAY_WORK_ORDERS_BOARD_ID are not set — "
            "run scripts/seed_monday.py first"
        )


@pytest.mark.parametrize("target", [DEALS_TARGET, WORK_ORDERS_TARGET], ids=lambda t: t.key)
def test_board_resolves_by_name(live_client, target):
    reader = BoardReader(live_client)
    ref = reader.resolve_board(target.board_name)
    assert ref.id


def test_deals_verification_passes(live_client):
    reader = BoardReader(live_client)
    result = read_deals_workbook(REPO_ROOT / DEALS_TARGET.workbook_filename)
    board_id = reader.resolve_board(DEALS_TARGET.board_name).id
    report = SeedingReport()

    verify_board(DEALS_TARGET, result, board_id, reader=reader, report=report)

    for check in report.verification:
        print(f"[{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.detail}")
    assert report.verification_passed


def test_work_orders_verification_passes(live_client):
    reader = BoardReader(live_client)
    result = read_work_orders_workbook(REPO_ROOT / WORK_ORDERS_TARGET.workbook_filename)
    board_id = reader.resolve_board(WORK_ORDERS_TARGET.board_name).id
    report = SeedingReport()

    verify_board(WORK_ORDERS_TARGET, result, board_id, reader=reader, report=report)

    for check in report.verification:
        print(f"[{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.detail}")
    assert report.verification_passed
