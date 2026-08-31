"""Fetch once, normalize once, cache: analytics' only input.

Mirrors `BoardReader`'s cache shape (plan section 3.4, fetch-once-compute-locally) one
layer up: `BoardReader` caches the *raw* API payload, `BoardRepository` caches the
*normalized* frame plus its quality report, on the same TTL. Two caches, not one, because
if monday.com goes down mid-session the raw cache alone would force re-normalizing on
every question; keeping both means a normalization bug never destroys the only copy of
data that can no longer be re-fetched (see `boards.py`'s own docstring for the same
argument one layer down).

Analytics (`bi_agent/analytics/`) never calls `BoardReader` directly — only through here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from bi_agent.data.normalize import NormalizedBoard, normalize_deals, normalize_work_orders
from bi_agent.data.quality import DataQualityReport, build_quality_report
from bi_agent.data.schema import DEALS_FIELDS, WORK_ORDERS_FIELDS
from bi_agent.monday.boards import BoardReader

__all__ = ["BoardData", "BoardRepository"]

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BoardData:
    normalized: NormalizedBoard
    quality: DataQualityReport
    fetched_at: datetime

    @property
    def frame(self):
        return self.normalized.frame


@dataclass
class _Entry:
    data: BoardData
    fetched_at: datetime


class BoardRepository:
    """`deals()` / `work_orders()`: normalized frame + quality report, cached."""

    def __init__(
        self,
        reader: BoardReader,
        *,
        now: Callable[[], datetime] = _utcnow,
        ttl_seconds: int | None = None,
    ) -> None:
        self._reader = reader
        self._now = now
        self._ttl = ttl_seconds if ttl_seconds is not None else reader.ttl_seconds
        self._cache: dict[str, _Entry] = {}

    def deals(self, *, force_refresh: bool = False) -> BoardData:
        return self._get(
            "deals",
            board_name="Deals",
            normalize=normalize_deals,
            fields=DEALS_FIELDS,
            force_refresh=force_refresh,
        )

    def work_orders(self, *, force_refresh: bool = False) -> BoardData:
        return self._get(
            "work_orders",
            board_name="Work Orders",
            normalize=normalize_work_orders,
            fields=WORK_ORDERS_FIELDS,
            force_refresh=force_refresh,
        )

    def invalidate(self, board: str | None = None) -> None:
        if board is None:
            self._cache.clear()
        else:
            self._cache.pop(board, None)

    # --- internals ---

    def _get(self, key: str, *, board_name: str, normalize, fields, force_refresh: bool) -> BoardData:
        if not force_refresh:
            cached = self._fresh_entry(key)
            if cached is not None:
                return cached.data

        started = time.perf_counter()
        snapshot = self._reader.fetch_items(board_name, force_refresh=force_refresh)
        normalized = normalize(snapshot)
        quality = build_quality_report(normalized, fields)
        data = BoardData(normalized=normalized, quality=quality, fetched_at=self._now())

        logger.info(
            "normalized board %r: %d rows (%d junk excluded) in %.0fms",
            board_name,
            quality.n_total_rows,
            normalized.n_junk_rows,
            (time.perf_counter() - started) * 1000,
        )

        self._cache[key] = _Entry(data=data, fetched_at=data.fetched_at)
        return data

    def _fresh_entry(self, key: str) -> _Entry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self._ttl <= 0:
            return None
        age = (self._now() - entry.fetched_at).total_seconds()
        return entry if age < self._ttl else None
