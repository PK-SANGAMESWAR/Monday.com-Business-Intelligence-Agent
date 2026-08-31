"""Cases 49-52: the real API. Deselected by default; run with `-m live`.

What these prove and what they do not:

**They prove** that authentication works end to end, that the response envelope is
shaped the way the authored fixtures assume, and that complexity is reported.
That is the whole point — the offline suite is built on fixtures that were
*authored* from documented shapes, and an envelope error in those fixtures would
otherwise stay invisible until F03.

**They do not prove** anything about the Deals or Work Orders boards, because
those do not exist yet (feature doc section 7). Whatever board this account has is
enough to validate the envelope, and F03 re-records against the real boards and
re-runs the whole suite unchanged.

Run:  uv run pytest -m live -v
"""

from __future__ import annotations

import logging

import pytest

from bi_agent.monday.boards import BoardReader
from bi_agent.monday.client import MondayClient
from bi_agent.monday.queries import BOARD_ITEMS_FIRST, LIST_BOARDS, ME

pytestmark = pytest.mark.live


@pytest.fixture
def live_client(live_settings):
    with MondayClient(live_settings) as client:
        yield client


@pytest.fixture
def board_with_items(live_client) -> str:
    """The id of a board that actually has rows on it.

    Picking the account's *first* board is not good enough: this account's first
    board is an empty subitems board, and validating an item envelope against
    zero items validates nothing at all. The first run of these tests passed
    exactly that way, which is the failure mode this fixture removes.
    """
    boards = live_client.execute(LIST_BOARDS, {"limit": 100})["boards"]
    if not boards:
        pytest.skip("this account has no boards to read")

    for board in boards:
        data = live_client.execute(
            BOARD_ITEMS_FIRST, {"boardIds": [str(board["id"])], "limit": 1}
        )
        page = (data["boards"][0] or {}).get("items_page") or {}
        if page.get("items"):
            return str(board["id"])

    pytest.skip(
        "no board in this account has any items, so the item envelope cannot be "
        "validated live; F03 seeds the real boards and re-runs this"
    )


def test_me_confirms_authentication(live_client):
    """Case 49: the token works, and we learn the account id."""
    data = live_client.execute(ME)

    assert data["me"]["id"]
    print(f"\naccount id: {data['me']['id']}, is_admin: {data['me']['is_admin']}")


def test_list_boards_returns_the_accounts_boards(live_client):
    """Case 50: board discovery by name, which is how F03 will find its work."""
    data = live_client.execute(LIST_BOARDS, {"limit": 100})
    boards = data["boards"]

    assert isinstance(boards, list)
    for board in boards:
        assert board["id"] and board["name"]
    print(f"\n{len(boards)} boards: " + ", ".join(f"{b['name']} ({b['id']})" for b in boards))


def test_real_envelope_matches_the_authored_fixture_shape(
    live_client, load_fixture, board_with_items
):
    """Case 51: **the reason these tests exist.**

    Compares the live response envelope against the shape the offline suite was
    built on. If monday.com nests `items_page` differently, or omits `cursor`, or
    returns `column_values` as an object, every offline test is passing against a
    fiction — and this is where that is discovered.
    """
    data = live_client.execute(
        BOARD_ITEMS_FIRST, {"boardIds": [board_with_items], "limit": 100}
    )

    assert isinstance(data["boards"], list) and data["boards"], "boards must be a list"
    board = data["boards"][0]
    assert {"id", "name", "columns", "items_page"} <= set(board)

    for column in board["columns"]:
        assert {"id", "title", "type"} <= set(column)

    page = board["items_page"]
    assert "cursor" in page, "pagination depends on cursor being present, even when null"
    assert isinstance(page["items"], list)
    assert page["items"], "the board must have items, or the next loop asserts nothing"

    for item in page["items"]:
        assert {"id", "name", "column_values"} <= set(item)
        assert isinstance(item["column_values"], list)
        for value in item["column_values"]:
            # Both `text` and `value` must be present: F04 needs the raw JSON for
            # dates and numbers, and losing it means re-recording every fixture.
            assert {"id", "type", "text", "value"} <= set(value)

    # And the same assertions must hold of the authored fixture, or the two are
    # not comparable and this test is proving nothing.
    authored = load_fixture("board_items_single_page")["data"]["boards"][0]
    assert set(board) >= set(authored) - {"items_page"}
    print(
        f"\nenvelope OK: board {board['name']!r}, {len(board['columns'])} columns, "
        f"{len(page['items'])} items, cursor={page['cursor']!r}"
    )


def test_complexity_is_reported_and_logged(live_client, caplog, board_with_items):
    """Case 52: NFR-7. Spend is observable, and we learn the real budget."""
    with caplog.at_level(logging.DEBUG):
        data = live_client.execute(
            BOARD_ITEMS_FIRST, {"boardIds": [board_with_items], "limit": 100}
        )

    complexity = data["complexity"]
    assert complexity["before"] > 0
    assert complexity["after"] <= complexity["before"]
    assert any("complexity" in record.getMessage() for record in caplog.records)
    print(
        f"\ncomplexity: before={complexity['before']} query={complexity['query']} "
        f"after={complexity['after']}"
    )


def test_board_reader_paginates_a_real_board(live_client, board_with_items):
    """The full read path against the real API, not just the transport."""
    reader = BoardReader(live_client)
    snapshot = reader.fetch_items(board_with_items)

    assert snapshot.source == "live"
    assert snapshot.page_count >= 1
    assert snapshot.item_count == len(snapshot.items)
    assert snapshot.item_count > 0
    assert snapshot.column_index, "column titles must resolve to IDs"

    cached = reader.fetch_items(board_with_items)
    assert cached.source == "cache"

    print(
        f"\n{snapshot.board_name!r}: {snapshot.item_count} items over "
        f"{snapshot.page_count} page(s); columns: "
        + ", ".join(sorted(snapshot.column_index)[:8])
    )
