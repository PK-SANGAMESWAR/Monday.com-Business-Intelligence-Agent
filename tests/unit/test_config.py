"""F01 test plan cases 1-11: Settings behaviour."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bi_agent.config import Settings, get_settings, load_settings, reset_settings_cache
from bi_agent.errors import ConfigError
from tests.conftest import FAKE_ANTHROPIC_KEY, FAKE_MONDAY_TOKEN


# --- 1: full happy path ------------------------------------------------------


def test_loads_when_all_env_vars_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONDAY_API_KEY", FAKE_MONDAY_TOKEN)
    monkeypatch.setenv("MONDAY_API_URL", "https://example.invalid/v2")
    monkeypatch.setenv("MONDAY_API_VERSION", "2025-01")
    monkeypatch.setenv("MONDAY_DEALS_BOARD_ID", "123456789")
    monkeypatch.setenv("MONDAY_WORK_ORDERS_BOARD_ID", "987654321")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC_KEY)
    monkeypatch.setenv("BI_AGENT_MODEL", "claude-opus-5")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("MAX_RETRIES", "5")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    settings = load_settings()

    assert settings.monday_api_key.get_secret_value() == FAKE_MONDAY_TOKEN
    assert settings.monday_api_url == "https://example.invalid/v2"
    assert settings.monday_api_version == "2025-01"
    assert settings.monday_deals_board_id == 123456789
    assert settings.monday_work_orders_board_id == 987654321
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == FAKE_ANTHROPIC_KEY
    assert settings.model == "claude-opus-5"
    assert settings.cache_ttl_seconds == 60
    assert settings.http_timeout_seconds == 12.5
    assert settings.max_retries == 5
    assert settings.log_level == "DEBUG"


# --- 2: missing token --------------------------------------------------------


def test_missing_monday_api_key_raises_config_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "MONDAY_API_KEY" in str(excinfo.value)
    assert excinfo.value.user_message
    # The point of the translation: a founder-facing tool must not answer
    # "you forgot the token" with a pydantic stack trace.
    assert not isinstance(excinfo.value, ValidationError)


# --- 3: blank token ----------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_monday_api_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv("MONDAY_API_KEY", blank)

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "MONDAY_API_KEY" in str(excinfo.value)


def test_implausibly_short_token_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONDAY_API_KEY", "abc")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "MONDAY_API_KEY" in str(excinfo.value)


# --- 4: defaults -------------------------------------------------------------


def test_defaults(monday_token: str) -> None:
    settings = load_settings()

    assert settings.monday_api_url == "https://api.monday.com/v2"
    assert settings.monday_api_version == "2024-10"
    assert settings.model == "claude-sonnet-5"
    assert settings.cache_ttl_seconds == 300
    assert settings.http_timeout_seconds == 30.0
    assert settings.max_retries == 3
    assert settings.log_level == "INFO"


# --- 5: env overrides defaults -----------------------------------------------


def test_env_overrides_default(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", "60")

    assert load_settings().cache_ttl_seconds == 60


def test_model_accepts_both_env_names(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan section 9.2 documents BI_AGENT_MODEL; MODEL is the field-name default."""
    monkeypatch.setenv("MODEL", "claude-haiku-4-5-20251001")
    assert load_settings().model == "claude-haiku-4-5-20251001"

    monkeypatch.setenv("BI_AGENT_MODEL", "claude-opus-5")
    assert load_settings().model == "claude-opus-5"


# --- 6: secrets do not render ------------------------------------------------


def test_secret_never_appears_in_repr_or_dump(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC_KEY)
    settings = load_settings()

    rendered = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        str(settings.describe()),
    ]
    for text in rendered:
        assert FAKE_MONDAY_TOKEN not in text
        assert FAKE_ANTHROPIC_KEY not in text
        assert "**********" in text


def test_secret_not_leaked_by_unrelated_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain str token would land in the traceback the first time an unrelated
    field failed validation. SecretStr is what prevents that."""
    monkeypatch.setenv("MONDAY_API_KEY", FAKE_MONDAY_TOKEN)
    monkeypatch.setenv("MAX_RETRIES", "not-a-number")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert FAKE_MONDAY_TOKEN not in str(excinfo.value)
    assert FAKE_MONDAY_TOKEN not in repr(excinfo.value)
    assert FAKE_MONDAY_TOKEN not in str(excinfo.value.__cause__)


# --- 7: optional Anthropic key -----------------------------------------------


def test_anthropic_key_optional(monday_token: str) -> None:
    settings = load_settings()

    assert settings.anthropic_api_key is None
    assert settings.has_anthropic_key is False


def test_blank_anthropic_key_is_treated_as_absent(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")

    assert load_settings().anthropic_api_key is None


# --- 8: board IDs absent -----------------------------------------------------


def test_board_ids_default_to_none(monday_token: str) -> None:
    settings = load_settings()

    assert settings.monday_deals_board_id is None
    assert settings.monday_work_orders_board_id is None
    assert settings.boards_configured is False


def test_blank_board_id_is_treated_as_absent(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONDAY_DEALS_BOARD_ID", "")

    assert load_settings().monday_deals_board_id is None


# --- 9: non-numeric board ID -------------------------------------------------


def test_non_numeric_board_id_raises_config_error(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONDAY_DEALS_BOARD_ID", "not-a-board")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "MONDAY_DEALS_BOARD_ID" in str(excinfo.value)


def test_invalid_log_level_raises_config_error(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "CHATTY")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "LOG_LEVEL" in str(excinfo.value)


# --- 10: caching -------------------------------------------------------------


def test_get_settings_is_cached_and_clearable(monday_token: str) -> None:
    first = get_settings()
    assert get_settings() is first

    reset_settings_cache()
    assert get_settings() is not first


# --- 11: whitespace around the key name, as in the real .env -----------------


def test_env_file_key_with_trailing_space_parses(tmp_path) -> None:
    """Regression guard. The real .env is written 'MONDAY_API_KEY =<token>'.

    Verified against python-dotenv on 2026-08-31: it strips whitespace around
    the key name. Pinned here so a loader change cannot silently break the file
    the developer actually has on disk.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(f"MONDAY_API_KEY ={FAKE_MONDAY_TOKEN}\n", encoding="utf-8")

    settings = load_settings(_env_file=str(env_file))

    assert settings.monday_api_key.get_secret_value() == FAKE_MONDAY_TOKEN


def test_env_file_value_with_surrounding_space_is_stripped(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"MONDAY_API_KEY =  {FAKE_MONDAY_TOKEN}  \n", encoding="utf-8")

    settings = load_settings(_env_file=str(env_file))

    assert settings.monday_api_key.get_secret_value() == FAKE_MONDAY_TOKEN


# --- supporting behaviour ----------------------------------------------------


def test_describe_lists_every_field(monday_token: str) -> None:
    described = load_settings().describe()

    for field in Settings.model_fields:
        assert field in described


def test_secret_values_returns_live_secrets_for_the_log_filter(
    monday_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC_KEY)

    assert set(load_settings().secret_values()) == {
        FAKE_MONDAY_TOKEN,
        FAKE_ANTHROPIC_KEY,
    }


# --- token shape edge cases ---------------------------------------------------


def test_token_with_embedded_whitespace_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token pasted across a line break is a real failure mode."""
    monkeypatch.setenv("MONDAY_API_KEY", FAKE_MONDAY_TOKEN[:30] + "\n" + FAKE_MONDAY_TOKEN[30:])

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "MONDAY_API_KEY" in str(excinfo.value)
    assert FAKE_MONDAY_TOKEN[:30] not in str(excinfo.value)


@pytest.mark.parametrize("value", [None, 12345])
def test_non_string_token_is_rejected_without_echoing_it(value: object) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings(monday_api_key=value)

    assert "MONDAY_API_KEY" in str(excinfo.value)


def test_non_string_log_level_is_rejected(monday_token: str) -> None:
    """LOG_LEVEL always arrives as a string from the environment; anything else
    is a programming error and must surface as ConfigError, not a crash later."""
    with pytest.raises(ConfigError) as excinfo:
        load_settings(log_level=20)

    assert "LOG_LEVEL" in str(excinfo.value)


def test_config_error_names_where_to_get_the_token() -> None:
    """"MONDAY_API_KEY is not set" without saying where to get one is half an
    error message."""
    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    assert "My Access Tokens" in str(excinfo.value)


def test_config_error_reports_every_problem_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONDAY_API_KEY", FAKE_MONDAY_TOKEN)
    monkeypatch.setenv("MAX_RETRIES", "not-a-number")
    monkeypatch.setenv("LOG_LEVEL", "CHATTY")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "MAX_RETRIES" in message
    assert "LOG_LEVEL" in message
