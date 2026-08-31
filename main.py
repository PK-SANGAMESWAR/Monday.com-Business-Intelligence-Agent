"""Setup check: resolve configuration and print it with secrets redacted.

Run this first after cloning:

    uv run python main.py

It answers the only question that matters before any feature works — "is my
environment wired up?" — and it is deliberately the one place that proves the
redaction guarantee holds against the real token.
"""

from __future__ import annotations

import logging
import sys

from bi_agent import __version__
from bi_agent.config import Settings, get_settings
from bi_agent.errors import ConfigError
from bi_agent.logging_config import configure_logging


def _print_settings(settings: Settings) -> None:
    described = settings.describe()
    width = max(len(name) for name in described)
    for name, value in described.items():
        print(f"  {name:<{width}}  {value}")


def _print_readiness(settings: Settings) -> None:
    print("\nReadiness:")
    print("  monday.com token          loaded")
    if settings.boards_configured:
        print("  board IDs                 configured")
    else:
        print("  board IDs                 not set yet (created and recorded by F03)")
    if settings.has_anthropic_key:
        print("  Anthropic key             loaded")
    else:
        print("  Anthropic key             not set yet (first needed by F06)")


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print("Configuration check FAILED\n", file=sys.stderr)
        print(exc.user_message, file=sys.stderr)
        return 1

    configure_logging(settings.log_level, secrets=settings.secret_values())
    logging.getLogger("bi_agent.setup").info(
        "configuration loaded", extra={"request_id": "setup"}
    )

    print(f"bi_agent {__version__} - configuration check passed\n")
    print("Resolved settings (secrets redacted):")
    _print_settings(settings)
    _print_readiness(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
