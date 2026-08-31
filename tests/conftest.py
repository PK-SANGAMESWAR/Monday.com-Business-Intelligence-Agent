"""Shared test fixtures.

The single most important guarantee here: **no test reads the developer's real
`.env` and no test sees the developer's real environment variables.** Without
that, the suite passes locally, fails in CI, and — worst case — exercises a live
token by accident (NFR-3, NFR-4).

F02 adds three more guarantees, all of which exist to keep the suite fast and
deterministic:

* **Time is injected, never observed.** `fake_clock` and `recorded_sleep` mean the
  TTL and backoff tests assert on *scheduling decisions* rather than on elapsed
  wall-clock seconds. A test that proves a 5-minute cache expires must not take
  five minutes, and one that proves exponential backoff must not take 14 seconds.
* **The network is not merely unused, it is absent.** `respx` intercepts the
  transport, so a test that accidentally omits a mock fails loudly instead of
  quietly reaching monday.com.
* **Fixtures are the API contract.** Every response body is a file on disk that
  `scripts/record_fixtures.py` can regenerate from the live API, so "our
  assumption about the envelope" and "the envelope" stay comparable.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bi_agent.config import Settings, reset_settings_cache

#: Every environment variable the Settings model can read. Cleared before each
#: test so a developer's shell cannot influence a result.
MANAGED_ENV_VARS = tuple(
    sorted(
        {name.upper() for name in Settings.model_fields}
        | {"BI_AGENT_MODEL", "MODEL"}
    )
)

#: A syntactically plausible token that is obviously not a real one.
FAKE_MONDAY_TOKEN = "eyJhbGciOiJIUzI1NiJ9.FAKE-monday-token-for-tests.0123456789abcdef"
FAKE_ANTHROPIC_KEY = "sk-ant-api03-FAKE-anthropic-key-for-tests-0123456789"

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: Where the mocked transport answers. Must match `Settings.monday_api_url`.
API_URL = "https://api.monday.com/v2"

#: The instant `fake_clock` starts at. Fixed, so `fetched_at` assertions and any
#: rendered "as of" string are byte-stable across runs and machines.
FROZEN_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Detach settings from the ambient environment and from `.env`."""
    for name in MANAGED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    # Settings reads `env_file` from model_config at instantiation time, so
    # patching the dict entry disables .env loading for every construction in
    # the test — including ones inside production code we call.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def monday_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a valid-looking MONDAY_API_KEY and return it."""
    monkeypatch.setenv("MONDAY_API_KEY", FAKE_MONDAY_TOKEN)
    return FAKE_MONDAY_TOKEN


# --- F02: fixtures, clock, sleep, client --------------------------------------


@pytest.fixture
def load_fixture() -> Callable[[str], dict[str, Any]]:
    """Load a recorded monday.com response body by file name.

    Returns a fresh copy each call: several tests mutate the payload to build a
    variant, and a shared dict would let one test corrupt another.
    """

    def _load(name: str) -> dict[str, Any]:
        path = FIXTURE_DIR / (name if name.endswith(".json") else f"{name}.json")
        if not path.exists():
            available = ", ".join(sorted(p.name for p in FIXTURE_DIR.glob("*.json")))
            raise FileNotFoundError(f"no fixture {path.name!r}; have: {available}")
        return json.loads(path.read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    """Build `Settings` with a fake token and any overrides a test needs."""

    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "monday_api_key": FAKE_MONDAY_TOKEN,
            "monday_api_url": API_URL,
            "max_retries": 3,
            "http_timeout_seconds": 5.0,
            "cache_ttl_seconds": 300,
        }
        values.update(overrides)
        return Settings(**values)

    return _make


class RecordedSleep:
    """A stand-in for `time.sleep` that records instead of waiting.

    Backoff is a *scheduling* decision. Asserting on the recorded delays tests
    that decision exactly, in microseconds, with no flakiness from a loaded CI
    machine — and a test that hangs for the real duration teaches nothing extra.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.delays)

    def __len__(self) -> int:
        return len(self.delays)


@pytest.fixture
def recorded_sleep() -> RecordedSleep:
    return RecordedSleep()


class FakeClock:
    """A monotonic-enough clock a test can move by hand."""

    def __init__(self, start: datetime = FROZEN_NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def monday_client_factory(
    respx_mock: Any,
    settings_factory: Callable[..., Settings],
    recorded_sleep: RecordedSleep,
) -> Iterator[Callable[..., tuple[Any, Any]]]:
    """Build a `MondayClient` wired to a mocked transport.

    Returns ``(client, route)``. The route is handed back so a test can assert on
    call count — "how many requests did that take" is the assertion behind the
    retry cases and the cache cases alike.
    """
    from bi_agent.monday.client import MondayClient

    created: list[Any] = []

    def _make(*, jitter: Callable[[], float] | None = None, **overrides: Any):
        settings = settings_factory(**overrides)
        route = respx_mock.post(settings.monday_api_url)
        client = MondayClient(
            settings,
            sleep=recorded_sleep,
            # Deterministic "jitter" by default: a test asserting on delays
            # cannot do so against a real random draw. The randomness itself is
            # tested separately, in its own case.
            jitter=jitter or (lambda: 1.0),
        )
        created.append(client)
        return client, route

    yield _make

    for client in created:
        client.close()


# --- F04: normalized-board fixtures, built from the live-recorded boards -----------------

LIVE_FIXTURE_DIR = FIXTURE_DIR / "live"


def _board_snapshot_from_live_fixture(name: str, *, respx_mock, settings_factory, recorded_sleep):
    """A real `BoardSnapshot` built from a recording of the actual seeded board
    (`scripts/record_fixtures.py --board ...`, F04 plan section 5 step 1), routed through
    `MondayClient`/`BoardReader` exactly as production code does — the only thing mocked
    is the transport.
    """
    from bi_agent.monday.boards import BoardReader
    from bi_agent.monday.client import MondayClient

    payload = json.loads((LIVE_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    board_name = payload["data"]["boards"][0]["name"]

    settings = settings_factory()
    route = respx_mock.post(settings.monday_api_url)
    route.mock(return_value=httpx.Response(200, json=payload))
    client = MondayClient(settings, sleep=recorded_sleep, jitter=lambda: 1.0)
    snapshot = BoardReader(client).fetch_items(board_name)
    client.close()
    return snapshot


@pytest.fixture
def deals_snapshot(respx_mock, settings_factory, recorded_sleep):
    return _board_snapshot_from_live_fixture(
        "deals_board_items.json",
        respx_mock=respx_mock,
        settings_factory=settings_factory,
        recorded_sleep=recorded_sleep,
    )


@pytest.fixture
def work_orders_snapshot(respx_mock, settings_factory, recorded_sleep):
    return _board_snapshot_from_live_fixture(
        "work_orders_board_items.json",
        respx_mock=respx_mock,
        settings_factory=settings_factory,
        recorded_sleep=recorded_sleep,
    )


@pytest.fixture
def board_repository(respx_mock, settings_factory, recorded_sleep, fake_clock):
    """A real `BoardRepository`, routed to both live-recorded boards.

    `LIST_BOARDS` returns the real account listing (both boards present);
    `BOARD_ITEMS_FIRST` is routed by the requested board id to whichever recording
    matches - the same two-board account F06's tools need to answer against either
    `describe_data("deals")` or `describe_data("work_orders")`.
    """
    from bi_agent.data.repository import BoardRepository
    from bi_agent.monday.boards import BoardReader
    from bi_agent.monday.client import MondayClient

    list_boards = json.loads((LIVE_FIXTURE_DIR / "list_boards.json").read_text(encoding="utf-8"))
    deals_payload = json.loads((LIVE_FIXTURE_DIR / "deals_board_items.json").read_text(encoding="utf-8"))
    wo_payload = json.loads((LIVE_FIXTURE_DIR / "work_orders_board_items.json").read_text(encoding="utf-8"))
    by_board_id = {
        deals_payload["data"]["boards"][0]["id"]: deals_payload,
        wo_payload["data"]["boards"][0]["id"]: wo_payload,
    }

    def _handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, variables = body["query"], body.get("variables") or {}
        if "ListBoards" in query:
            return httpx.Response(200, json=list_boards)
        if "BoardItemsFirst" in query:
            board_id = str(variables["boardIds"][0])
            return httpx.Response(200, json=by_board_id[board_id])
        raise AssertionError(f"unhandled query in board_repository fixture: {query[:80]!r}")

    settings = settings_factory()
    respx_mock.post(settings.monday_api_url).mock(side_effect=_handle)
    client = MondayClient(settings, sleep=recorded_sleep, jitter=lambda: 1.0)
    reader = BoardReader(client, now=fake_clock)
    repo = BoardRepository(reader, now=fake_clock)

    yield repo

    client.close()
