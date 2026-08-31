"""Integration tests for scripts/seed_monday.py against a fake monday.com.

`FakeMondayServer` is a minimal in-memory stand-in that understands the five
read documents F02 sends and the five write documents this seeder sends. It
lets these tests exercise the real orchestration in `seed_monday.py` —
board creation, default-column deletion, item creation, resume, schema
mismatch refusal — through `respx`, with no real network and no real
monday.com account.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bi_agent.monday.boards import BoardReader
from bi_agent.monday.client import MondayClient
from scripts.seed_monday import (
    DEALS_TARGET,
    print_dry_run,
    seed_board,
    verify_board,
)
from scripts.seeding.errors import SeedError
from scripts.seeding.mutations import CREATE_BOARD
from scripts.seeding.report import SeedingReport
from scripts.seeding.workbook import WorkbookReadResult, WorkbookRow
from scripts.seeding.writer import Pacer, SeedWriter


# --- a tiny, hand-built stand-in for the Deals workbook -----------------------


def _tiny_deals_result(rows: list[WorkbookRow] | None = None) -> WorkbookReadResult:
    all_rows = [
        WorkbookRow(
            source_row=2,
            values={
                "Deal Name": "Alpha",
                "Owner code": "OWNER_001",
                "Client Code": "COMPANY001",
                "Deal Status": "Won",
                "Close Date (A)": None,
                "Closure Probability": "High",
                "Masked Deal value": 100000,
                "Tentative Close Date": None,
                "Deal Stage": "G. Project Won",
                "Product deal": "Pure Service",
                "Sector/service": "Mining",
                "Created Date": None,
            },
        ),
        WorkbookRow(
            source_row=3,
            values={
                "Deal Name": "Beta",
                "Owner code": "OWNER_002",
                "Client Code": "COMPANY002",
                "Deal Status": "Open",
                "Close Date (A)": None,
                "Closure Probability": None,
                "Masked Deal value": 0,
                "Tentative Close Date": None,
                "Deal Stage": "A. Lead Generated",
                "Product deal": None,
                "Sector/service": "Renewables",
                "Created Date": None,
            },
        ),
        WorkbookRow(
            source_row=52,
            values={
                "Deal Name": "Nezuko",
                "Owner code": "",
                "Client Code": None,
                "Deal Status": "Deal Status",
                "Close Date (A)": "Close Date (A)",
                "Closure Probability": "Closure Probability",
                "Masked Deal value": "",
                "Tentative Close Date": "Tentative Close Date",
                "Deal Stage": "Deal Stage",
                "Product deal": "Product deal",
                "Sector/service": "Sector/service",
                "Created Date": "Created Date",
            },
            is_junk=True,
        ),
    ]
    chosen = all_rows if rows is None else rows
    return WorkbookReadResult(
        sheet_name="Deal tracker",
        headers=list(all_rows[0].values.keys()),
        rows=chosen,
        dropped_empty_rows=[348],
        junk_rows=[row.source_row for row in chosen if row.is_junk],
    )


# --- a minimal fake monday.com over HTTP --------------------------------------


class FakeMondayServer:
    def __init__(self) -> None:
        self.boards: dict[str, dict[str, Any]] = {}
        self._next_id = 1000

    def _new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def seed_board(
        self,
        board_id: str,
        name: str,
        columns: list[tuple[str, str, str]] | None = None,
        *,
        with_default_item: bool = False,
    ) -> None:
        self.boards[board_id] = {
            "id": board_id,
            "name": name,
            "columns": [
                {"id": cid, "title": title, "type": ctype}
                for cid, title, ctype in (columns or [])
            ],
            "items": (
                [{"id": self._new_id(), "name": "Task 1", "column_values": []}]
                if with_default_item
                else []
            ),
        }

    def _board_payload(self, board: dict[str, Any]) -> dict[str, Any]:
        return {"id": board["id"], "name": board["name"], "columns": board["columns"]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        variables = payload.get("variables") or {}

        if "ListBoards" in query:
            data = {
                "boards": [
                    {"id": board["id"], "name": board["name"]}
                    for board in self.boards.values()
                ]
            }
        elif "BoardColumns" in query:
            board = self.boards.get(str(variables["boardIds"][0]))
            data = {"boards": [self._board_payload(board)] if board else []}
        elif "BoardItemsFirst" in query:
            board = self.boards[str(variables["boardIds"][0])]
            data = {
                "complexity": {"before": 989970, "after": 989960, "query": 10},
                "boards": [
                    {
                        **self._board_payload(board),
                        "items_page": {"cursor": None, "items": board["items"]},
                    }
                ],
            }
        elif "CreateBoard" in query:
            board_id = self._new_id()
            self.seed_board(
                board_id,
                variables["name"],
                # A real monday.com board's `columns` field lists the
                # mandatory, undeletable "name" column alongside the real
                # deletable defaults — the exact shape that broke the first
                # live run (DeleteMandatoryColumnException on "name").
                columns=[
                    ("name", "Name", "name"),
                    ("person_x", "Owner", "people"),
                    ("status_x", "Status", "status"),
                ],
                # A real new board also carries one auto-generated sample
                # item ("Task 1") that broke the first live run by inflating
                # every item count — the exact scenario resolve_or_create_board
                # must clean up before writing anything of its own.
                with_default_item=True,
            )
            data = {"create_board": {"id": board_id, "name": variables["name"]}}
        elif "CreateColumn" in query:
            board_id = str(variables["boardId"])
            column_id = self._new_id()
            self.boards[board_id]["columns"].append(
                {"id": column_id, "title": variables["title"], "type": variables["columnType"]}
            )
            data = {"create_column": {"id": column_id, "title": variables["title"]}}
        elif "DeleteColumn" in query:
            board_id, column_id = str(variables["boardId"]), str(variables["columnId"])
            if column_id == "name":
                return httpx.Response(
                    200,
                    json={
                        "errors": [{"message": "Cannot delete mandatory column"}],
                        "data": {"delete_column": None},
                    },
                )
            self.boards[board_id]["columns"] = [
                c for c in self.boards[board_id]["columns"] if c["id"] != column_id
            ]
            data = {"delete_column": {"id": column_id}}
        elif "CreateItem" in query:
            board_id = str(variables["boardId"])
            board = self.boards[board_id]
            item_id = self._new_id()
            values = json.loads(variables["columnValues"])
            column_values = []
            for column in board["columns"]:
                if column["id"] not in values:
                    continue
                raw = values[column["id"]]
                text = raw.get("date", "") if isinstance(raw, dict) else str(raw)
                column_values.append(
                    {"id": column["id"], "type": column["type"], "text": text, "value": json.dumps(raw)}
                )
            item = {"id": item_id, "name": variables["itemName"], "column_values": column_values}
            board["items"].append(item)
            data = {"create_item": {"id": item_id, "name": item["name"]}}
        elif "DeleteItem" in query:
            item_id = str(variables["itemId"])
            for board in self.boards.values():
                board["items"] = [i for i in board["items"] if str(i["id"]) != item_id]
            data = {"delete_item": {"id": item_id}}
        elif "DeleteBoard" in query:
            board_id = str(variables["boardId"])
            del self.boards[board_id]
            data = {"delete_board": {"id": board_id}}
        else:
            raise AssertionError(f"FakeMondayServer: unhandled query: {query[:120]!r}")

        return httpx.Response(200, json={"data": data})


@pytest.fixture
def fake_server() -> FakeMondayServer:
    return FakeMondayServer()


@pytest.fixture
def wired(respx_mock, settings_factory, fake_server, recorded_sleep):
    settings = settings_factory()
    respx_mock.post(settings.monday_api_url).mock(side_effect=fake_server.handle)

    read_client = MondayClient(settings, sleep=recorded_sleep, jitter=lambda: 1.0)
    writer = SeedWriter(settings, sleep=recorded_sleep, jitter=lambda: 1.0)
    reader = BoardReader(read_client)

    yield reader, writer, fake_server

    read_client.close()
    writer.close()


def _seed(reader, writer, result, *, recreate=False, confirm=lambda p: True, report=None):
    return seed_board(
        DEALS_TARGET,
        result,
        reader=reader,
        writer=writer,
        pacer=Pacer(None),
        recreate=recreate,
        confirm=confirm,
        report=report if report is not None else SeedingReport(),
    )


# --- case 75/76/77: a full run on a clean account -----------------------------


def test_full_run_creates_board_deletes_defaults_creates_columns_and_items(wired):
    reader, writer, server = wired
    result = _tiny_deals_result()
    report = SeedingReport()

    board_id = _seed(reader, writer, result, report=report)

    board = server.boards[board_id]
    expected_titles = {spec.header for spec in DEALS_TARGET.columns} | {"Source Row"}
    non_name_titles = {c["title"] for c in board["columns"] if c["id"] != "name"}
    assert non_name_titles == expected_titles
    assert any(c["id"] == "name" for c in board["columns"])  # mandatory, left alone
    assert not any(c["title"] in ("Owner", "Status") for c in board["columns"])

    assert len(board["items"]) == 3  # includes the junk row: decision D-1
    counts = report.boards[0]
    assert counts.created == 3
    assert counts.junk_rows == [52]


def test_stray_default_item_is_deleted_before_seeding(wired):
    """The bug that broke the first live run: a new board's auto-generated
    "Task 1" sample item is invisible to the columns query and must be
    deleted before any workbook row is written, or every count is off by one."""
    reader, writer, server = wired
    board_id = _seed(reader, writer, _tiny_deals_result())

    names = [item["name"] for item in server.boards[board_id]["items"]]
    assert "Task 1" not in names
    assert len(server.boards[board_id]["items"]) == 3


def test_column_order_matches_workbook_order(wired):
    reader, writer, server = wired
    board_id = _seed(reader, writer, _tiny_deals_result())
    titles = [c["title"] for c in server.boards[board_id]["columns"] if c["id"] != "name"]
    expected = [spec.header for spec in DEALS_TARGET.columns] + ["Source Row"]
    assert titles == expected


# --- case 78: dry run, zero requests ------------------------------------------


def test_dry_run_issues_zero_requests(respx_mock, capsys):
    result = _tiny_deals_result()
    print_dry_run((DEALS_TARGET,), {"deals": result}, 30.0)
    out = capsys.readouterr().out
    assert "DRY RUN: zero requests were sent." in out
    assert "3" in out  # row count printed
    assert respx_mock.calls.call_count == 0


# --- case 79: second run is idempotent ----------------------------------------


def test_second_run_on_a_complete_board_is_a_no_op(wired):
    reader, writer, server = wired
    result = _tiny_deals_result()

    board_id_1 = _seed(reader, writer, result)
    report2 = SeedingReport()
    board_id_2 = _seed(reader, writer, result, report=report2)

    assert board_id_1 == board_id_2
    assert report2.boards[0].created == 0
    assert report2.boards[0].skipped_existing == 3
    assert len(server.boards[board_id_2]["items"]) == 3


# --- case 80: resume after interruption ---------------------------------------


def test_resume_creates_only_the_missing_rows(wired):
    reader, writer, server = wired
    full = _tiny_deals_result()
    first_two = _tiny_deals_result(rows=full.rows[:2])

    board_id = _seed(reader, writer, first_two)
    assert len(server.boards[board_id]["items"]) == 2

    report = SeedingReport()
    _seed(reader, writer, full, report=report)

    assert report.boards[0].created == 1
    assert report.boards[0].skipped_existing == 2
    assert len(server.boards[board_id]["items"]) == 3


# --- case 81: existing board with mismatched columns is refused --------------


def test_existing_board_with_missing_columns_is_refused(wired):
    reader, writer, server = wired
    server.seed_board("42", "Deals", columns=[("t1", "Owner code", "text")])

    with pytest.raises(SeedError):
        _seed(reader, writer, _tiny_deals_result())

    assert server.boards["42"]["items"] == []  # nothing was written


# --- case 82: --recreate without confirmation is refused ----------------------


def test_recreate_without_confirmation_is_refused_and_nothing_is_deleted(wired):
    reader, writer, server = wired
    result = _tiny_deals_result()
    board_id = _seed(reader, writer, result)

    with pytest.raises(SeedError):
        _seed(reader, writer, result, recreate=True, confirm=lambda p: False)

    assert board_id in server.boards  # not deleted


def test_recreate_with_confirmation_deletes_and_rebuilds(wired):
    reader, writer, server = wired
    result = _tiny_deals_result()
    original_id = _seed(reader, writer, result)

    report = SeedingReport()
    new_id = _seed(reader, writer, result, recreate=True, confirm=lambda p: True, report=report)

    assert original_id not in server.boards
    assert len(server.boards[new_id]["items"]) == 3
    assert report.boards[0].created == 3


# --- case 69: zero is written, never omitted, at the orchestration level -----


def test_zero_deal_value_is_written_not_omitted(wired):
    reader, writer, server = wired
    board_id = _seed(reader, writer, _tiny_deals_result())

    board = server.boards[board_id]
    beta = next(item for item in board["items"] if item["name"] == "Beta")
    value_column_id = next(
        c["id"] for c in board["columns"] if c["title"] == "Masked Deal value"
    )
    cv = next(cv for cv in beta["column_values"] if cv["id"] == value_column_id)
    assert cv["text"] == "0"


# --- case 93/94/95 style: verification round-trips through F02 ---------------


def test_verification_passes_against_the_fake_board(wired):
    reader, writer, server = wired
    result = _tiny_deals_result()
    report = SeedingReport()

    board_id = _seed(reader, writer, result, report=report)
    verify_board(DEALS_TARGET, result, board_id, reader=reader, report=report)

    assert report.verification_passed, report.verification


def test_verification_catches_a_short_board(wired):
    """If a board is missing rows, verification must fail, not pass quietly."""
    reader, writer, server = wired
    result = _tiny_deals_result()
    report = SeedingReport()

    board_id = _seed(reader, writer, result, report=report)
    server.boards[board_id]["items"].pop()  # simulate a dropped write

    verify_board(DEALS_TARGET, result, board_id, reader=reader, report=report)
    assert not report.verification_passed


# --- write transport: retry and pacing ----------------------------------------


def test_seed_writer_retries_after_429_then_succeeds(respx_mock, settings_factory, recorded_sleep):
    settings = settings_factory(max_retries=2)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"errors": [{"message": "rate limit exceeded"}]})
        return httpx.Response(200, json={"data": {"create_board": {"id": "1", "name": "Deals"}}})

    respx_mock.post(settings.monday_api_url).mock(side_effect=handler)
    writer = SeedWriter(settings, sleep=recorded_sleep, jitter=lambda: 1.0)
    try:
        data = writer.execute(CREATE_BOARD, {"name": "Deals", "kind": "public"})
    finally:
        writer.close()

    assert data["create_board"]["id"] == "1"
    assert len(recorded_sleep) == 1
    assert calls["n"] == 2


def test_pacer_waits_to_hold_the_configured_rate():
    ticks = iter([0.0, 0.0])
    sleeps: list[float] = []
    pacer = Pacer(60.0, monotonic=lambda: next(ticks), sleep=sleeps.append)  # 1 item/sec

    pacer.wait()  # first call: nothing to wait for
    pacer.wait()  # elapsed 0.0s against a 1.0s interval -> sleeps 1.0s

    assert sleeps == [1.0]


def test_pacer_disabled_when_items_per_minute_is_none():
    sleeps: list[float] = []
    pacer = Pacer(None, monotonic=lambda: 0.0, sleep=sleeps.append)
    pacer.wait()
    pacer.wait()
    assert sleeps == []
