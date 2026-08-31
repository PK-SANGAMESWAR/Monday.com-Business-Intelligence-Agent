"""Shared test fixtures.

The single most important guarantee here: **no test reads the developer's real
`.env` and no test sees the developer's real environment variables.** Without
that, the suite passes locally, fails in CI, and — worst case — exercises a live
token by accident (NFR-3, NFR-4).
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
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
