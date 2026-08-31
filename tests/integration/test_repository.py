"""Integration tests for bi_agent/data/repository.py: fetch -> normalize -> cache."""

from __future__ import annotations

import json

import httpx
import pytest

from bi_agent.data.repository import BoardRepository
from bi_agent.monday.boards import BoardReader
from bi_agent.monday.client import MondayClient

LIVE_FIXTURE = "tests/fixtures/live/deals_board_items.json"


@pytest.fixture
def wired_repository(respx_mock, settings_factory, recorded_sleep, fake_clock):
    payload = json.loads(open(LIVE_FIXTURE, encoding="utf-8").read())
    settings = settings_factory(cache_ttl_seconds=300)
    route = respx_mock.post(settings.monday_api_url)
    route.mock(return_value=httpx.Response(200, json=payload))

    client = MondayClient(settings, sleep=recorded_sleep, jitter=lambda: 1.0)
    reader = BoardReader(client, now=fake_clock)
    repo = BoardRepository(reader, now=fake_clock)

    yield repo, route, fake_clock

    client.close()


#: Every `BoardReader.fetch_items` call issues `resolve_board` (LIST_BOARDS) unconditionally
#: plus BOARD_ITEMS_FIRST when its own cache is stale — so one *uncached* repository fetch
#: costs 2 requests, and a repository-cache hit that still finds a fresh `BoardReader` cache
#: underneath costs 1 (LIST_BOARDS only). See bi_agent/monday/boards.py::fetch_items.


def test_deals_fetches_and_normalizes(wired_repository):
    repo, route, _clock = wired_repository
    data = repo.deals()

    assert len(data.frame) == 346
    assert data.quality.n_junk_rows_excluded == 2
    assert route.call_count == 2


def test_second_call_within_ttl_is_cached(wired_repository):
    """Cached at the *repository* layer: `BoardReader.fetch_items` is not even called,
    so not even the LIST_BOARDS resolve happens again."""
    repo, route, _clock = wired_repository
    repo.deals()
    repo.deals()
    assert route.call_count == 2


def test_force_refresh_bypasses_cache(wired_repository):
    repo, route, _clock = wired_repository
    repo.deals()
    repo.deals(force_refresh=True)
    assert route.call_count == 4


def test_cache_expires_after_ttl(wired_repository):
    repo, route, clock = wired_repository
    repo.deals()
    clock.advance(301)
    repo.deals()
    assert route.call_count == 4


def test_invalidate_forces_a_refetch(wired_repository):
    """Invalidating the repository cache does not invalidate `BoardReader`'s own cache,
    so the re-fetch still resolves the board name but skips re-fetching its items."""
    repo, route, _clock = wired_repository
    repo.deals()
    repo.invalidate("deals")
    repo.deals()
    assert route.call_count == 3
