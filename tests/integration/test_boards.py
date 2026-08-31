"""Cases 30-48: board resolution, cursor pagination, the TTL cache, staleness.

Case 31 is the one that matters most. FR-3 says "read **all** data from both
boards", and the failure mode it guards against is silent: a client that stops at
the first page returns 500 perfectly valid rows out of 520 and reports no error at
all, so every downstream number is quietly wrong. "All" is asserted here, not
assumed.

The cache cases are the other half of FR-16. Plan section 4.3 requires the agent
to serve stale data *and say it is stale*, which is only expressible if staleness
survives the call boundary as data — hence `BoardSnapshot.source`. A client that
quietly returns old rows is precisely the failure that field exists to prevent.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from bi_agent.errors import (
    MondayQueryError,
    MondayUnavailableError,
    SchemaMismatchError,
)
from bi_agent.monday.boards import BoardReader, BoardSnapshot
from bi_agent.monday.queries import REGISTRY

BOARD_ID = "9876543210"
BOARD_NAME = "Deals"

ALL_ITEM_NAMES = [
    "Sakura",
    "Alphonse",
    "Nezuko",
    "Dolphin",
    "Whale",
    "Octopus",
    "Turtle",
    "Golden fish",
]


def ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _document_name(query_text: str) -> str:
    for name, document in REGISTRY.items():
        if document.text == query_text:
            return name
    raise AssertionError(f"a document outside the registry was sent: {query_text!r}")


def plan_responses(route, **plan: Any) -> None:
    """Answer each registry document from its own queue of responses.

    Board fetching sends several *different* documents to one URL, so a flat
    `side_effect` list would couple every test to the exact call order of the
    implementation. Dispatching on the document keeps these tests about
    behaviour: "when asked for page two, this is page two".
    """
    queues = {
        name: iter(value if isinstance(value, list) else [value])
        for name, value in plan.items()
    }

    def handler(request: httpx.Request) -> httpx.Response:
        name = _document_name(json.loads(request.content)["query"])
        if name not in queues:
            raise AssertionError(f"unexpected document sent: {name}")
        try:
            return next(queues[name])
        except StopIteration:
            raise AssertionError(f"{name} was sent more times than planned") from None

    route.mock(side_effect=handler)


@pytest.fixture
def reader_factory(monday_client_factory, fake_clock):
    """A `BoardReader` over a mocked client, with time under the test's control."""

    def _make(*, max_pages: int | None = None, **overrides: Any):
        client, route = monday_client_factory(**overrides)
        kwargs: dict[str, Any] = {"now": fake_clock}
        if max_pages is not None:
            kwargs["max_pages"] = max_pages
        return BoardReader(client, **kwargs), route

    return _make


# --- cases 30-34: pagination --------------------------------------------------


def test_single_page_board(reader_factory, load_fixture):
    """Case 30: the common case - one request, every row."""
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    snapshot = reader.fetch_items(BOARD_ID)

    assert snapshot.page_count == 1
    assert snapshot.item_count == 3
    assert [item["name"] for item in snapshot.items] == ALL_ITEM_NAMES[:3]
    assert route.call_count == 1


def test_three_page_board_returns_every_item_in_order(reader_factory, load_fixture):
    """Case 31: the FR-3 test. All items, in order, no duplicates, no drops."""
    reader, route = reader_factory()
    plan_responses(
        route,
        BOARD_ITEMS_FIRST=ok(load_fixture("board_items_page1")),
        BOARD_ITEMS_NEXT=[
            ok(load_fixture("board_items_page2")),
            ok(load_fixture("board_items_page3")),
        ],
    )

    snapshot = reader.fetch_items(BOARD_ID)

    names = [item["name"] for item in snapshot.items]
    assert names == ALL_ITEM_NAMES
    assert len(set(item["id"] for item in snapshot.items)) == len(snapshot.items)
    assert snapshot.page_count == 3
    assert snapshot.item_count == 8
    assert route.call_count == 3


def test_cursor_is_threaded_into_the_next_request(reader_factory, load_fixture):
    """Case 32: the loop follows the server's cursor and stops when it is null."""
    reader, route = reader_factory()
    plan_responses(
        route,
        BOARD_ITEMS_FIRST=ok(load_fixture("board_items_page1")),
        BOARD_ITEMS_NEXT=[
            ok(load_fixture("board_items_page2")),
            ok(load_fixture("board_items_page3")),
        ],
    )

    reader.fetch_items(BOARD_ID)

    cursors = [
        json.loads(call.request.content)["variables"].get("cursor")
        for call in route.calls
    ]
    assert cursors == [None, "CURSOR_PAGE_2", "CURSOR_PAGE_3"]


def test_empty_board_is_an_empty_snapshot_not_an_error(reader_factory, load_fixture):
    """Case 33: a board with no rows is a fact about the business, not a fault."""
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_empty")))

    snapshot = reader.fetch_items(BOARD_ID)

    assert snapshot.items == []
    assert snapshot.item_count == 0
    assert snapshot.page_count == 1
    assert snapshot.columns, "columns still resolve for an empty board"


def test_runaway_cursor_hits_the_page_cap(reader_factory, load_fixture):
    """Case 34: a server that always returns a cursor must not hang the agent.

    Without a cap this is an infinite loop inside a web request — the worst
    possible failure, because it never surfaces as an error at all.
    """
    reader, route = reader_factory(max_pages=4)
    plan_responses(
        route,
        BOARD_ITEMS_FIRST=ok(load_fixture("board_items_page1")),
        BOARD_ITEMS_NEXT=[ok(load_fixture("board_items_runaway"))] * 3,
    )

    with pytest.raises(MondayQueryError) as excinfo:
        reader.fetch_items(BOARD_ID)

    assert "page" in str(excinfo.value).lower()
    assert route.call_count == 4


# --- cases 35-38: board resolution --------------------------------------------


def test_resolve_board_by_name_is_case_insensitive(reader_factory, load_fixture):
    """Case 35: board IDs do not exist until F03, so names must work."""
    reader, route = reader_factory()
    plan_responses(route, LIST_BOARDS=[ok(load_fixture("list_boards"))] * 3)

    for spelling in ("Deals", "deals", "  DEALS  "):
        assert reader.resolve_board(spelling).id == BOARD_ID


def test_resolve_board_by_id_does_not_list_boards(reader_factory, load_fixture):
    """Case 36: once `Settings` has an ID, discovery is wasted complexity spend."""
    reader, route = reader_factory()
    plan_responses(route)  # any request at all is a failure

    for given in (BOARD_ID, int(BOARD_ID)):
        assert reader.resolve_board(given).id == BOARD_ID

    assert route.call_count == 0


def test_unknown_board_name_names_the_boards_that_do_exist(
    reader_factory, load_fixture
):
    """Case 37: this is the message a user sees when seeding has not run, so it
    has to be specific enough to act on."""
    reader, route = reader_factory()
    plan_responses(route, LIST_BOARDS=ok(load_fixture("list_boards_without_target")))

    with pytest.raises(MondayQueryError) as excinfo:
        reader.resolve_board("Deals")

    message = str(excinfo.value)
    assert "Deals" in message
    assert "Your first board" in message
    assert "Deals" in excinfo.value.user_message


def test_duplicate_board_names_resolve_deterministically_and_are_reported(
    reader_factory, load_fixture, caplog
):
    """Case 38: picking at random would make every answer irreproducible."""
    reader, route = reader_factory()
    plan_responses(route, LIST_BOARDS=[ok(load_fixture("list_boards_duplicate_names"))] * 2)

    with caplog.at_level(logging.WARNING):
        first = reader.resolve_board("Deals")
    second = reader.resolve_board("Deals")

    assert first.id == second.id == BOARD_ID  # lowest id wins, every time
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert "9876543299" in warning and "9876543210" in warning


def test_missing_board_id_is_a_query_error(reader_factory, load_fixture):
    """A board that was deleted comes back as an empty list, HTTP 200."""
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_missing")))

    with pytest.raises(MondayQueryError):
        reader.fetch_items(BOARD_ID)


# --- cases 39-40: columns -----------------------------------------------------


def test_column_titles_map_to_ids(reader_factory, load_fixture):
    """Case 39: NFR-8. Callers ask for titles; opaque IDs never leave this layer."""
    reader, route = reader_factory()
    plan_responses(route, BOARD_COLUMNS=ok(load_fixture("board_columns")))

    columns = reader.fetch_columns(BOARD_ID)
    index = {column.title: column.id for column in columns}

    assert index["Deal Value"] == "numeric_mkq1val"
    assert index["Sector/service"] == "dropdown_mkq1sect"
    assert index["Created Date"] == "date_mkq1crt"


def test_snapshot_carries_the_column_index(reader_factory, load_fixture):
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    snapshot = reader.fetch_items(BOARD_ID)

    assert snapshot.column_index["Deal Status"] == "status"
    assert snapshot.column_id("Deal Value") == "numeric_mkq1val"
    assert snapshot.column_id("No Such Column") is None


def test_require_columns_lists_every_missing_title_at_once(
    reader_factory, load_fixture
):
    """Case 40: two of three absent, and the error names **both**.

    Reporting one missing column at a time turns a single fix into three rounds
    of run-fail-fix, and F01 already puts the whole list in the user message.
    """
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))
    snapshot = reader.fetch_items(BOARD_ID)

    snapshot.require_columns(["Deal Value", "Deal Status"])  # present: no raise

    with pytest.raises(SchemaMismatchError) as excinfo:
        snapshot.require_columns(["Deal Value", "Closure Probability", "Close Date"])

    assert excinfo.value.missing == ["Closure Probability", "Close Date"]
    assert "Closure Probability" in excinfo.value.user_message
    assert "Close Date" in excinfo.value.user_message


# --- cases 41-47: the TTL cache and staleness ---------------------------------


def test_second_fetch_within_ttl_is_served_from_cache(reader_factory, load_fixture):
    """Case 41: NFR-1 and NFR-6. One board fetch per TTL window, not per question."""
    reader, route = reader_factory(cache_ttl_seconds=300)
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    first = reader.fetch_items(BOARD_ID)
    second = reader.fetch_items(BOARD_ID)

    assert route.call_count == 1
    assert first.source == "live"
    assert second.source == "cache"
    assert second.items == first.items
    assert second.fetched_at == first.fetched_at


def test_fetch_after_ttl_expiry_goes_back_to_the_api(
    reader_factory, load_fixture, fake_clock
):
    """Case 42: the clock is injected, so this test takes no time at all."""
    reader, route = reader_factory(cache_ttl_seconds=300)
    plan_responses(
        route, BOARD_ITEMS_FIRST=[ok(load_fixture("board_items_single_page"))] * 2
    )

    first = reader.fetch_items(BOARD_ID)
    fake_clock.advance(301)
    second = reader.fetch_items(BOARD_ID)

    assert route.call_count == 2
    assert second.source == "live"
    assert second.fetched_at > first.fetched_at


def test_ttl_boundary_is_inclusive_of_the_cached_window(
    reader_factory, load_fixture, fake_clock
):
    """One second before expiry is still fresh; a TTL that fires early is a bug
    that only shows up as a doubled API bill."""
    reader, route = reader_factory(cache_ttl_seconds=300)
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    reader.fetch_items(BOARD_ID)
    fake_clock.advance(299)

    assert reader.fetch_items(BOARD_ID).source == "cache"
    assert route.call_count == 1


def test_force_refresh_bypasses_a_valid_cache_entry(reader_factory, load_fixture):
    """Case 43: the UI's refresh button has to actually refresh."""
    reader, route = reader_factory(cache_ttl_seconds=300)
    plan_responses(
        route, BOARD_ITEMS_FIRST=[ok(load_fixture("board_items_single_page"))] * 2
    )

    reader.fetch_items(BOARD_ID)
    refreshed = reader.fetch_items(BOARD_ID, force_refresh=True)

    assert route.call_count == 2
    assert refreshed.source == "live"


def test_api_failure_with_a_stale_entry_serves_it_labelled_as_stale(
    reader_factory, load_fixture, fake_clock
):
    """Case 44: serving stale data *labelled stale* beats failing, and beats
    serving it silently. `fetched_at` is preserved so the agent can say its age."""
    reader, route = reader_factory(cache_ttl_seconds=300, max_retries=0)
    plan_responses(
        route,
        BOARD_ITEMS_FIRST=[
            ok(load_fixture("board_items_single_page")),
            httpx.Response(503, json=load_fixture("server_error")),
        ],
    )

    fresh = reader.fetch_items(BOARD_ID)
    fake_clock.advance(3600)
    degraded = reader.fetch_items(BOARD_ID)

    assert degraded.source == "stale-cache"
    assert degraded.items == fresh.items
    assert degraded.fetched_at == fresh.fetched_at
    assert degraded.age_seconds(fake_clock()) == 3600


def test_api_failure_without_a_cache_entry_propagates(reader_factory, load_fixture):
    """Case 45: with nothing to fall back on, F06 degrades per plan section 4.3.

    Inventing an empty snapshot here would turn "monday.com is down" into
    "you have no deals", which is the single worst answer available.
    """
    reader, route = reader_factory(max_retries=0)
    plan_responses(
        route, BOARD_ITEMS_FIRST=httpx.Response(503, json=load_fixture("server_error"))
    )

    with pytest.raises(MondayUnavailableError):
        reader.fetch_items(BOARD_ID)


def test_invalidate_forces_the_next_call_to_refetch(reader_factory, load_fixture):
    """Case 46."""
    reader, route = reader_factory(cache_ttl_seconds=300)
    plan_responses(
        route, BOARD_ITEMS_FIRST=[ok(load_fixture("board_items_single_page"))] * 2
    )

    reader.fetch_items(BOARD_ID)
    reader.invalidate(BOARD_ID)
    again = reader.fetch_items(BOARD_ID)

    assert route.call_count == 2
    assert again.source == "live"


def test_invalidate_all_clears_every_board(reader_factory, load_fixture):
    reader, route = reader_factory(cache_ttl_seconds=300)
    other = "9876543211"
    plan_responses(
        route, BOARD_ITEMS_FIRST=[ok(load_fixture("board_items_single_page"))] * 4
    )

    reader.fetch_items(BOARD_ID)
    reader.fetch_items(other)
    reader.invalidate()

    assert reader.fetch_items(BOARD_ID).source == "live"
    assert reader.fetch_items(other).source == "live"
    assert route.call_count == 4


def test_two_boards_are_cached_independently(reader_factory, load_fixture):
    """Case 47: refreshing Deals must not silently drop Work Orders."""
    reader, route = reader_factory(cache_ttl_seconds=300)
    other = "9876543211"
    plan_responses(
        route, BOARD_ITEMS_FIRST=[ok(load_fixture("board_items_single_page"))] * 3
    )

    reader.fetch_items(BOARD_ID)
    reader.fetch_items(other)
    reader.fetch_items(BOARD_ID, force_refresh=True)

    assert reader.fetch_items(other).source == "cache"
    assert route.call_count == 3


# --- case 48: the payload survives the round trip -----------------------------


def test_item_fields_survive_the_round_trip(reader_factory, load_fixture):
    """Case 48: F04 needs `text` *and* `value`; losing either is silent data loss.

    F02 returns monday.com's shape deliberately — no renaming, no coercion. The
    boundary is the point: if this layer starts knowing what a deal value is, the
    schema ends up split across two features.
    """
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    snapshot = reader.fetch_items(BOARD_ID)
    item = snapshot.items[0]

    assert item["id"] == "1001"
    assert item["name"] == "Sakura"

    by_id = {column["id"]: column for column in item["column_values"]}
    assert by_id["numeric_mkq1val"]["text"] == "1250000"
    assert by_id["numeric_mkq1val"]["value"] == '"1250000"'
    assert by_id["date_mkq1crt"]["text"] == "2024-08-14"
    assert json.loads(by_id["date_mkq1crt"]["value"]) == {"date": "2024-08-14"}
    assert by_id["status"]["type"] == "status"

    # A null `value` is preserved as null rather than coerced to "".
    empty = [
        column
        for column in snapshot.items[1]["column_values"]
        if column["id"] == "numeric_mkq1val"
    ][0]
    assert empty["value"] is None
    assert empty["text"] == ""


def test_snapshot_identifies_its_board(reader_factory, load_fixture):
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    snapshot = reader.fetch_items(BOARD_ID)

    assert isinstance(snapshot, BoardSnapshot)
    assert snapshot.board_id == BOARD_ID
    assert snapshot.board_name == BOARD_NAME
    assert snapshot.fetched_at.tzinfo is not None, "naive timestamps cannot be compared"


def test_fetch_items_accepts_a_board_name(reader_factory, load_fixture):
    """The agent should not have to resolve a board before it can read one."""
    reader, route = reader_factory()
    plan_responses(
        route,
        LIST_BOARDS=ok(load_fixture("list_boards")),
        BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")),
    )

    snapshot = reader.fetch_items("Deals")

    assert snapshot.board_id == BOARD_ID
    assert route.call_count == 2


def test_board_fetch_logs_one_summary_line(reader_factory, load_fixture, caplog):
    """NFR-7: pages, items and duration for a completed fetch, in one line."""
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    with caplog.at_level(logging.INFO):
        reader.fetch_items(BOARD_ID)

    summaries = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "Deals" in record.getMessage()
    ]
    assert len(summaries) == 1
    assert "3 items" in summaries[0]
    assert "1 page" in summaries[0]


# --- malformed payloads: drop nothing silently --------------------------------


def test_stale_snapshot_reports_itself_as_stale(reader_factory, load_fixture, fake_clock):
    """`is_stale` is what the UI branches on when it renders the age caveat."""
    reader, route = reader_factory(cache_ttl_seconds=300, max_retries=0)
    plan_responses(
        route,
        BOARD_ITEMS_FIRST=[
            ok(load_fixture("board_items_single_page")),
            httpx.Response(503, json=load_fixture("server_error")),
        ],
    )

    fresh = reader.fetch_items(BOARD_ID)
    assert fresh.is_stale is False

    fake_clock.advance(3600)
    assert reader.fetch_items(BOARD_ID).is_stale is True


def test_board_list_of_the_wrong_shape_is_a_query_error(reader_factory):
    reader, route = reader_factory()
    plan_responses(route, LIST_BOARDS=ok({"data": {"boards": "not a list"}}))

    with pytest.raises(MondayQueryError):
        reader.resolve_board("Deals")


def test_items_page_of_the_wrong_shape_is_a_query_error(
    reader_factory, load_fixture
):
    payload = load_fixture("board_items_single_page")
    payload["data"]["boards"][0]["items_page"] = "not a page"

    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(payload))

    with pytest.raises(MondayQueryError):
        reader.fetch_items(BOARD_ID)


def test_items_that_are_not_a_list_is_a_query_error(reader_factory, load_fixture):
    """Coercing this to zero rows would report an empty board as fact."""
    payload = load_fixture("board_items_single_page")
    payload["data"]["boards"][0]["items_page"]["items"] = {"unexpected": True}

    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(payload))

    with pytest.raises(MondayQueryError):
        reader.fetch_items(BOARD_ID)


def test_absent_items_key_is_an_empty_page_not_an_error(reader_factory, load_fixture):
    """An omitted `items` key on a null cursor page is a legal empty result."""
    payload = load_fixture("board_items_single_page")
    del payload["data"]["boards"][0]["items_page"]["items"]

    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(payload))

    assert reader.fetch_items(BOARD_ID).items == []


def test_default_clock_produces_timezone_aware_timestamps(
    monday_client_factory, load_fixture
):
    """Without an injected clock the reader must still produce comparable times."""
    client, route = monday_client_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    snapshot = BoardReader(client).fetch_items(BOARD_ID)

    assert snapshot.fetched_at.tzinfo is not None
    assert snapshot.age_seconds() >= 0


def test_null_column_text_survives_untouched(reader_factory, load_fixture):
    """Reconciled against the live API on 2026-08-31.

    The authored fixtures originally modelled an unset column as ``text: ""``.
    The real API returns **both** forms on the same board: ``""`` for an unset
    people column and JSON ``null`` for an unset subtasks column. F02 must pass
    both through exactly as received — coercing null to "" here would erase the
    distinction before F04 can decide what it means, which is the "zero used as
    missing" trap the data profile warns about, one layer earlier.
    """
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    snapshot = reader.fetch_items(BOARD_ID)
    by_id = {c["id"]: c for c in snapshot.items[0]["column_values"]}

    assert by_id["subitems"]["text"] is None
    assert by_id["subitems"]["value"] is None
    # ...and the other spelling of "missing" is still its own thing.
    empty = {c["id"]: c for c in snapshot.items[1]["column_values"]}
    assert empty["numeric_mkq1val"]["text"] == ""


def test_a_column_may_exist_without_appearing_in_any_item(reader_factory, load_fixture):
    """Also reconciled from live: the `name` column is a column *definition* but
    never appears in `column_values` — the item's name lives on `item["name"]`.
    Column resolution must not assume the two sets match."""
    reader, route = reader_factory()
    plan_responses(route, BOARD_ITEMS_FIRST=ok(load_fixture("board_items_single_page")))

    snapshot = reader.fetch_items(BOARD_ID)

    assert snapshot.column_id("Deal Name") == "name"
    value_ids = {c["id"] for c in snapshot.items[0]["column_values"]}
    assert "name" not in value_ids
    assert snapshot.items[0]["name"] == "Sakura"
