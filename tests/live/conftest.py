"""Live tests need the real environment - the exact thing the root conftest hides.

The root `isolated_env` fixture is autouse and deliberately detaches every test
from `.env` and from the developer's shell (NFR-3/NFR-4). These tests are the one
category that must opt out, so the fixture is **overridden by name** here: pytest
resolves fixtures from the nearest conftest, so the root version simply does not
run for this directory. Overriding is safer than adding an escape hatch to the
root fixture, where a typo would silently expose the whole suite to a live token.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from bi_agent.config import Settings, load_settings, reset_settings_cache


@pytest.fixture(autouse=True)
def isolated_env() -> Iterator[None]:
    """Override: leave the real environment and `.env` in place."""
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(scope="function")
def live_settings() -> Settings:
    """Real settings, or skip. A missing token is not a test failure."""
    try:
        settings = load_settings()
    except Exception as exc:  # ConfigError, but any failure means "cannot run live"
        pytest.skip(f"live tests need a real MONDAY_API_KEY: {exc}")
    return settings
