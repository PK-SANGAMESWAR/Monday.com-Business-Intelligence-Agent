"""Board and column resolution, cursor pagination, and the raw TTL cache.

This module knows about boards, columns and cursors. It does not know what a deal
is: it returns monday.com's payload shape unchanged, and F04 owns the translation
to canonical fields. The boundary is deliberate — if this layer started knowing
what a "deal value" is, the schema would end up split across two features and
every column rename would need edits in both.

Three decisions are load-bearing enough to state here:

**Boards resolve by name as well as by ID.** Board IDs do not exist until F03
creates the boards, so a client that demanded an ID would be untestable until
then. It is also the behaviour we want permanently: an ID is meaningless to a
user, and the error message when a board is missing has to name what *was* found.

**Column IDs never leave this layer.** monday.com identifies columns by opaque
IDs like ``text_mkq1abc``, which change when a board is recreated. Callers ask for
titles; :class:`BoardSnapshot` holds the mapping (NFR-8).

**Staleness is data, not a log line.** Plan section 4.3 requires the agent to
serve cached rows *and say how old they are*. That is only possible if the fact
survives the call boundary, hence :attr:`BoardSnapshot.source` and
:attr:`BoardSnapshot.fetched_at`. A client that quietly returns old rows is the
exact failure these fields prevent.

The cache here holds the **raw** payload. F04's repository separately caches the
normalized frame, which looks like duplication until monday.com goes down: at
that point we need the last-known *rows*, and re-normalizing them is cheap and
deterministic. Coupling the two caches would mean a normalization bug destroys
the only copy of data we can no longer re-fetch.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from bi_agent.errors import MondayError, MondayQueryError, SchemaMismatchError
from bi_agent.monday.client import MondayClient
from bi_agent.monday.queries import (
    BOARD_COLUMNS,
    BOARD_ITEMS_FIRST,
    BOARD_ITEMS_NEXT,
    DEFAULT_PAGE_SIZE,
    LIST_BOARDS,
)

__all__ = ["BoardReader", "BoardRef", "BoardSnapshot", "Column"]

logger = logging.getLogger(__name__)

#: How many boards to list when resolving a name. The account has single digits;
#: this is a bound, not an expectation.
BOARD_LIST_LIMIT = 100

#: Hard stop on pagination. At 500 items a page this is 100k items — far beyond
#: the scale boundary recorded in plan section 10 — so hitting it means the
#: server is handing back a cursor forever, and looping inside a web request is a
#: hang, which is worse than an error because it never surfaces as one.
MAX_PAGES = 200

Source = Literal["live", "cache", "stale-cache"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BoardRef:
    """A board's identity. ``name`` is ``None`` when resolved from a bare ID."""

    id: str
    name: str | None = None


@dataclass(frozen=True)
class Column:
    """A board column as monday.com defines it: opaque id, human title, type."""

    id: str
    title: str
    type: str


@dataclass(frozen=True)
class BoardSnapshot:
    """Everything one board returned, plus how fresh it is.

    ``items`` are monday.com's own dicts, untouched: ``id``, ``name`` and
    ``column_values`` carrying both ``text`` (the display string) and ``value``
    (raw JSON preserving type information).
    """

    board_id: str
    board_name: str
    columns: list[Column]
    items: list[dict[str, Any]]
    fetched_at: datetime
    source: Source = "live"
    page_count: int = 0
    column_index: dict[str, str] = field(default_factory=dict)

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def is_stale(self) -> bool:
        return self.source == "stale-cache"

    def column_id(self, title: str) -> str | None:
        """The opaque ID for a column title, or ``None`` if the board lacks it."""
        return self.column_index.get(title)

    def age_seconds(self, now: datetime | None = None) -> float:
        """How old these rows are. The number the agent quotes when degrading."""
        return ((now or _utcnow()) - self.fetched_at).total_seconds()

    def require_columns(self, titles: Iterable[str]) -> dict[str, str]:
        """Return the title→ID map for ``titles``, or raise naming **all** gaps.

        Reporting one missing column at a time turns a single fix into three
        rounds of run-fail-fix, so every absent title is collected before
        raising; F01 already puts the whole list into the user-facing message.
        """
        wanted = list(titles)
        missing = [title for title in wanted if title not in self.column_index]
        if missing:
            raise SchemaMismatchError(
                f"board {self.board_name!r} ({self.board_id}) is missing "
                f"{len(missing)} expected column(s): {', '.join(missing)}",
                missing=missing,
            )
        return {title: self.column_index[title] for title in wanted}


@dataclass
class _CacheEntry:
    snapshot: BoardSnapshot
    fetched_at: datetime


class BoardReader:
    """Resolves boards and columns by name, and reads every item on a board."""

    def __init__(
        self,
        client: MondayClient,
        *,
        now: Callable[[], datetime] = _utcnow,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._client = client
        self._now = now
        self._page_size = page_size
        self._max_pages = max_pages
        self._cache: dict[str, _CacheEntry] = {}

    @property
    def ttl_seconds(self) -> int:
        return self._client.settings.cache_ttl_seconds

    # --- resolution ---

    def resolve_board(self, name_or_id: str | int) -> BoardRef:
        """Resolve a board by ID (used directly) or by name (listed and matched).

        Matching is case-insensitive and ignores surrounding whitespace, because
        the alternative is a user typing ``deals`` and being told no such board
        exists while looking at one.
        """
        given = str(name_or_id).strip()
        if given.isdigit():
            return BoardRef(id=given)

        boards = self._client.execute(LIST_BOARDS, {"limit": BOARD_LIST_LIMIT}).get(
            "boards"
        )
        if not isinstance(boards, list):
            raise MondayQueryError(
                "monday.com returned no board list, so the board named "
                f"{given!r} could not be resolved."
            )

        wanted = given.casefold()
        matches = [
            board
            for board in boards
            if str(board.get("name", "")).strip().casefold() == wanted
        ]

        if not matches:
            found = ", ".join(sorted(str(board.get("name")) for board in boards))
            message = (
                f"No monday.com board named {given!r} was found. "
                f"Boards in this account: {found or 'none'}."
            )
            raise MondayQueryError(message, user_message=message)

        # Lowest ID wins. Any deterministic rule would do; what matters is that
        # the same question does not resolve to a different board tomorrow.
        matches.sort(key=lambda board: str(board["id"]))
        chosen = matches[0]

        if len(matches) > 1:
            logger.warning(
                "%d boards are named %r (ids: %s); using %s. "
                "Set the board ID explicitly to remove the ambiguity.",
                len(matches),
                given,
                ", ".join(str(board["id"]) for board in matches),
                chosen["id"],
            )

        return BoardRef(id=str(chosen["id"]), name=str(chosen.get("name", given)))

    def fetch_columns(self, board: str | int) -> list[Column]:
        """The board's columns, for title→ID indirection."""
        ref = self.resolve_board(board)
        data = self._client.execute(BOARD_COLUMNS, {"boardIds": [ref.id]})
        payload = self._single_board(data, ref)
        return self._columns_from(payload)

    # --- items ---

    def fetch_items(
        self, board: str | int, *, force_refresh: bool = False
    ) -> BoardSnapshot:
        """Every item on a board, from cache when fresh, from the API otherwise.

        On an API failure with a cache entry past its TTL, the stale entry is
        returned labelled ``stale-cache`` rather than raised: an old number the
        user is told is old beats no answer, and beats an old number presented as
        current. With no entry at all the error propagates and F06 degrades per
        plan section 4.3.
        """
        ref = self.resolve_board(board)

        if not force_refresh:
            cached = self._fresh_entry(ref.id)
            if cached is not None:
                logger.debug(
                    "board %s served from cache (%.0fs old)",
                    ref.id,
                    cached.snapshot.age_seconds(self._now()),
                )
                return replace(cached.snapshot, source="cache")

        try:
            snapshot = self._fetch_all_pages(ref)
        except MondayError as exc:
            stale = self._cache.get(ref.id)
            if stale is None:
                raise
            logger.warning(
                "board %s could not be refreshed (%s); serving cached data "
                "from %s, %.0fs old",
                ref.id,
                exc,
                stale.snapshot.fetched_at.isoformat(),
                stale.snapshot.age_seconds(self._now()),
            )
            return replace(stale.snapshot, source="stale-cache")

        self._cache[ref.id] = _CacheEntry(snapshot, snapshot.fetched_at)
        return snapshot

    def invalidate(self, board_id: str | int | None = None) -> None:
        """Drop one board's cached rows, or every board's."""
        if board_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(board_id), None)

    # --- internals ---

    def _fresh_entry(self, board_id: str) -> _CacheEntry | None:
        entry = self._cache.get(board_id)
        if entry is None:
            return None
        age = (self._now() - entry.fetched_at).total_seconds()
        return entry if age < self.ttl_seconds else None

    def _fetch_all_pages(self, ref: BoardRef) -> BoardSnapshot:
        started = time.perf_counter()
        fetched_at = self._now()

        data = self._client.execute(
            BOARD_ITEMS_FIRST, {"boardIds": [ref.id], "limit": self._page_size}
        )
        board = self._single_board(data, ref)
        columns = self._columns_from(board)

        page = board.get("items_page") or {}
        items = list(self._items_from(page, ref))
        cursor = page.get("cursor")
        pages = 1

        while cursor:
            if pages >= self._max_pages:
                raise MondayQueryError(
                    f"board {ref.id} returned more than {self._max_pages} pages "
                    "of items; stopping rather than looping. The cursor is not "
                    "advancing, or the board is far larger than expected."
                )
            data = self._client.execute(
                BOARD_ITEMS_NEXT, {"cursor": cursor, "limit": self._page_size}
            )
            page = data.get("next_items_page") or {}
            items.extend(self._items_from(page, ref))
            cursor = page.get("cursor")
            pages += 1

        board_name = str(board.get("name") or ref.name or ref.id)
        elapsed_ms = (time.perf_counter() - started) * 1000

        logger.info(
            "fetched board %r (%s): %d items over %d page(s) in %.0fms",
            board_name,
            ref.id,
            len(items),
            pages,
            elapsed_ms,
        )

        return BoardSnapshot(
            board_id=str(board.get("id") or ref.id),
            board_name=board_name,
            columns=columns,
            items=items,
            fetched_at=fetched_at,
            source="live",
            page_count=pages,
            column_index={column.title: column.id for column in columns},
        )

    @staticmethod
    def _single_board(data: dict[str, Any], ref: BoardRef) -> dict[str, Any]:
        """The one board we asked for, or a specific error.

        A deleted or inaccessible board comes back as ``{"boards": []}`` with
        HTTP 200 — a success-shaped response for a request that failed.
        """
        boards = data.get("boards")
        if not isinstance(boards, list) or not boards:
            label = f"{ref.name!r} ({ref.id})" if ref.name else ref.id
            message = (
                f"monday.com returned no board for {label}. It may have been "
                "deleted, or this token may not have access to it."
            )
            raise MondayQueryError(message, user_message=message)
        return boards[0]

    @staticmethod
    def _columns_from(board: dict[str, Any]) -> list[Column]:
        return [
            Column(
                id=str(column.get("id", "")),
                title=str(column.get("title", "")),
                type=str(column.get("type", "")),
            )
            for column in board.get("columns") or []
            if isinstance(column, dict)
        ]

    @staticmethod
    def _items_from(page: Any, ref: BoardRef) -> Sequence[dict[str, Any]]:
        """Items from one page, insisting the page is shaped as documented.

        Silently treating an unexpected shape as "no items" would drop rows
        without an error, which plan section 3 calls out as a bug rather than
        graceful degradation.
        """
        if not isinstance(page, dict):
            raise MondayQueryError(
                f"board {ref.id} returned a page of items in an unexpected shape."
            )
        items = page.get("items")
        if items is None:
            return []
        if not isinstance(items, list):
            raise MondayQueryError(
                f"board {ref.id} returned items that are not a list."
            )
        return [item for item in items if isinstance(item, dict)]
