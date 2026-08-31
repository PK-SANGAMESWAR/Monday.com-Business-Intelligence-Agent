"""Environment-driven settings, validated once at startup.

This module is the only place in the package allowed to contain a literal
endpoint, timeout, model name or board ID (NFR-8). Everything else imports
:func:`get_settings`.

Two design points worth stating:

* Secrets are :class:`~pydantic.SecretStr`, never ``str``. pydantic renders them
  as ``**********`` in ``repr``, model dumps and validation-error output. A plain
  ``str`` token would land in a traceback the first time an *unrelated* field
  failed to validate.
* Validation failures surface as :class:`~bi_agent.errors.ConfigError`, never as
  pydantic's ``ValidationError``. A stack trace is not an acceptable answer to
  "you forgot the token".
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bi_agent.errors import ConfigError

__all__ = ["Settings", "get_settings", "load_settings", "reset_settings_cache"]

#: What a redacted secret looks like. Matches pydantic's own SecretStr rendering
#: so config output is consistent however it is produced.
SECRET_MASK = "**********"

#: Shown by :meth:`Settings.describe` for an optional value that is not set.
NOT_SET_DISPLAY = "(not set)"

#: Shortest plausible monday.com token. The real one is 228 characters; this is
#: a sanity floor that catches a truncated paste, not a format assertion.
MIN_TOKEN_LENGTH = 20

_VALID_LOG_LEVELS = frozenset(logging.getLevelNamesMapping())

#: How to obtain each credential, appended to the ConfigError message. Being
#: told "MONDAY_API_KEY is not set" without being told where to get one is only
#: half an error message.
_HOW_TO_OBTAIN = {
    "MONDAY_API_KEY": (
        "Create a token in monday.com under your avatar - Developers - "
        "My Access Tokens, then add it to .env (see .env.example)."
    ),
    "ANTHROPIC_API_KEY": (
        "Create a key at console.anthropic.com under API Keys, then add it to "
        ".env (see .env.example)."
    ),
}


class Settings(BaseSettings):
    """All runtime configuration, read from the environment and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # --- monday.com ---
    monday_api_key: SecretStr = Field(
        description="monday.com personal access token. Required."
    )
    monday_api_url: str = Field(default="https://api.monday.com/v2")
    monday_api_version: str = Field(
        default="2024-10",
        description=(
            "Sent as the API-Version header. Pinned so a server-side default "
            "change cannot silently alter response shapes."
        ),
    )

    # Board IDs do not exist until F03 creates the boards, so they are optional.
    # F02's board resolution must therefore also support lookup by name - which
    # is the behaviour we want anyway (plan section 2.4, column-ID indirection).
    monday_deals_board_id: int | None = Field(default=None)
    monday_work_orders_board_id: int | None = Field(default=None)

    # --- Anthropic ---
    # Optional so F01-F05 are not blocked behind a credential they do not use.
    # F06 validates it at the point of use.
    anthropic_api_key: SecretStr | None = Field(default=None)
    model: str = Field(
        default="claude-sonnet-5",
        validation_alias=AliasChoices("BI_AGENT_MODEL", "MODEL"),
    )

    # --- behaviour ---
    cache_ttl_seconds: int = Field(default=300, ge=0)
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    log_level: str = Field(default="INFO")

    # --- validators ---

    @field_validator("monday_api_key", mode="before")
    @classmethod
    def _validate_token(cls, value: Any) -> Any:
        """Presence and plausible shape. Never echoes the value."""
        if value is None:
            return value
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            return value
        token = raw.strip()
        if not token:
            raise ValueError("must not be empty - an empty token is a missing token")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"is implausibly short ({len(token)} characters); "
                "it looks truncated"
            )
        if any(char.isspace() for char in token):
            raise ValueError("must not contain whitespace")
        return token

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _blank_secret_is_absent(cls, value: Any) -> Any:
        """A variable present but blank in `.env` means "not set", not "empty"."""
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator(
        "monday_deals_board_id", "monday_work_orders_board_id", mode="before"
    )
    @classmethod
    def _blank_board_id_is_absent(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("monday_api_url", "monday_api_version", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _validate_log_level(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        level = value.strip().upper()
        if level not in _VALID_LOG_LEVELS:
            valid = ", ".join(sorted(_VALID_LOG_LEVELS))
            raise ValueError(f"must be one of: {valid}")
        return level

    # --- derived views ---

    @property
    def has_anthropic_key(self) -> bool:
        """F06 needs this; F01-F05 run happily without it."""
        return self.anthropic_api_key is not None

    @property
    def boards_configured(self) -> bool:
        """False until F03 has created both boards and recorded their IDs."""
        return (
            self.monday_deals_board_id is not None
            and self.monday_work_orders_board_id is not None
        )

    def secret_values(self) -> list[str]:
        """Live secret values, for :class:`SecretRedactionFilter` to scrub.

        The only place in the package that calls ``get_secret_value()`` outside
        of building an HTTP header.
        """
        return [
            value.get_secret_value()
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), SecretStr)
        ]

    def describe(self) -> dict[str, str]:
        """Every setting rendered for display, with secrets masked."""
        described: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                described[name] = SECRET_MASK
            elif value is None:
                described[name] = NOT_SET_DISPLAY
            else:
                described[name] = str(value)
        return described


def _env_var_name(loc: Any) -> str:
    """Map a pydantic error location back to the env var a user would set."""
    return _LOC_TO_ENV_VAR.get(str(loc).lower(), str(loc).upper())


def _build_loc_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            env_var = str(alias.choices[0])
            for choice in alias.choices:
                mapping[str(choice).lower()] = env_var
        else:
            env_var = name.upper()
        mapping[name.lower()] = env_var
    return mapping


_LOC_TO_ENV_VAR = _build_loc_map()


def _describe_validation_error(exc: ValidationError) -> str:
    """Turn pydantic's report into a message naming variables, not fields.

    Deliberately built from ``loc``/``type``/``msg`` only - never from ``input``,
    which would put the offending value (potentially a token) into the message.
    """
    problems: list[str] = []
    hints: list[str] = []
    for error in exc.errors():
        loc = error["loc"][0] if error["loc"] else "configuration"
        env_var = _env_var_name(loc)
        if error["type"] == "missing":
            problems.append(f"{env_var} is not set")
        else:
            problems.append(f"{env_var} is invalid: {error['msg']}")
        hint = _HOW_TO_OBTAIN.get(env_var)
        if hint and hint not in hints:
            hints.append(hint)

    summary = "Configuration is invalid. " + "; ".join(problems) + "."
    if hints:
        summary += " " + " ".join(hints)
    return summary


def load_settings(**overrides: Any) -> Settings:
    """Build :class:`Settings`, translating validation failures to ConfigError.

    Accepts pydantic-settings' private keywords (``_env_file=...``) so tests can
    point at a fixture file, or at nothing at all.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        message = _describe_validation_error(exc)
        raise ConfigError(message, user_message=message) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings. Read once, reused everywhere."""
    return load_settings()


def reset_settings_cache() -> None:
    """Drop the cached settings so a new environment takes effect.

    Used by tests, and by the Streamlit UI if configuration is ever reloaded.
    """
    get_settings.cache_clear()
