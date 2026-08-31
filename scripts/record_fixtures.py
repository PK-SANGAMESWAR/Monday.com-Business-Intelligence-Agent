"""Record live monday.com responses as test fixtures, and compare their shape.

Why this exists: F02's offline tests are only as good as the fixtures, and the
fixtures were **authored from monday.com's documented response shapes** rather
than recorded, because the Deals and Work Orders boards do not exist yet (feature
doc section 7). This script closes as much of that gap as can be closed now: it
records the *real* envelope from whatever board the account does have, and prints
a structural comparison against the authored fixtures. Shape errors then surface
here, cheaply, instead of in F03.

It is read-only. It runs entirely through `MondayClient`, so the read-only gate
applies to it exactly as it applies to the agent - this script cannot write to a
board any more than the agent can. It lives outside `bi_agent/` because it is not
part of the shipped package, and it must never be imported by it.

Usage::

    uv run python scripts/record_fixtures.py --list
    uv run python scripts/record_fixtures.py --board "Your first board"
    uv run python scripts/record_fixtures.py --board 1234567890 --compare

Recorded files land in `tests/fixtures/live/` and are **not** used by the offline
suite. Review them before committing: they contain whatever is on the board.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bi_agent.config import load_settings  # noqa: E402
from bi_agent.errors import BIAgentError  # noqa: E402
from bi_agent.logging_config import configure_logging  # noqa: E402
from bi_agent.monday.client import MondayClient  # noqa: E402
from bi_agent.monday.queries import (  # noqa: E402
    BOARD_COLUMNS,
    BOARD_ITEMS_FIRST,
    BOARD_ITEMS_NEXT,
    DEFAULT_PAGE_SIZE,
    LIST_BOARDS,
    ME,
)

AUTHORED_DIR = REPO_ROOT / "tests" / "fixtures"
LIVE_DIR = AUTHORED_DIR / "live"

#: Which authored fixture each recording should be compared against.
COMPARISONS = {
    "me.json": "me.json",
    "list_boards.json": "list_boards.json",
    "board_columns.json": "board_columns.json",
    "board_items_page1.json": "board_items_single_page.json",
}


def shape(value: Any, depth: int = 0) -> Any:
    """A payload reduced to its structure: keys and types, no values.

    This is the artefact worth comparing. Values differ between the starter board
    and the real boards and always will; a missing `cursor` key or a `column_values`
    that turns out to be an object rather than a list is what would break us.
    """
    if depth > 9:
        return "..."
    if isinstance(value, dict):
        return {key: shape(item, depth + 1) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def scrub(payload: Any, secrets: list[str]) -> Any:
    """Remove any secret that the API echoed back into a response body.

    monday.com includes request context in some error payloads. A recorded
    fixture is a file we intend to commit, so this is not optional.
    """
    if isinstance(payload, dict):
        return {key: scrub(item, secrets) for key, item in payload.items()}
    if isinstance(payload, list):
        return [scrub(item, secrets) for item in payload]
    if isinstance(payload, str):
        for secret in secrets:
            payload = payload.replace(secret, "***REDACTED***")
    return payload


def write(name: str, payload: Any, secrets: list[str]) -> Path:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = LIVE_DIR / name
    # Recorded as a full response envelope, matching the authored fixtures, so
    # the two are directly interchangeable in a test.
    body = {"data": scrub(payload, secrets)}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"  recorded {path.relative_to(REPO_ROOT)}")
    return path


def compare(recorded_name: str) -> None:
    """Print the structural difference between a recording and its authored twin."""
    authored_name = COMPARISONS.get(recorded_name)
    if authored_name is None:
        return

    recorded_path, authored_path = LIVE_DIR / recorded_name, AUTHORED_DIR / authored_name
    if not recorded_path.exists() or not authored_path.exists():
        return

    # Compare the `data` sub-tree on both sides, because that is precisely what
    # `MondayClient.execute` returns and therefore all any caller ever sees. The
    # surrounding envelope (`account_id` and friends) is discarded by the client,
    # so a difference there is not a difference that can affect us.
    recorded = shape(json.loads(recorded_path.read_text(encoding="utf-8")).get("data"))
    authored = shape(json.loads(authored_path.read_text(encoding="utf-8")).get("data"))

    print(f"\n  {recorded_name} vs authored {authored_name}:")
    if recorded == authored:
        print("    identical structure")
        return

    # Deliberately printed in full rather than diffed cleverly: this output is
    # evidence pasted into the feature doc, and a human decides what it means.
    print("    DIFFERENT structure")
    print("      live    :", json.dumps(recorded, indent=6)[:1500])
    print("      authored:", json.dumps(authored, indent=6)[:1500])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", help="board name or ID to record items from")
    parser.add_argument(
        "--list", action="store_true", help="list the account's boards and exit"
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="compare recorded structure against the authored fixtures",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE)
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except BIAgentError as exc:
        print(f"error: {exc.user_message}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level, secrets=settings.secret_values())
    secrets = settings.secret_values()

    with MondayClient(settings) as client:
        try:
            print("recording ME ...")
            me = client.execute(ME)
            print(f"  authenticated as {me['me']['name']} (id {me['me']['id']}), "
                  f"is_admin={me['me']['is_admin']}")
            write("me.json", me, secrets)

            print("recording LIST_BOARDS ...")
            boards = client.execute(LIST_BOARDS, {"limit": 100})
            for board in boards.get("boards", []):
                print(f"  board {board['id']}: {board['name']}")
            write("list_boards.json", boards, secrets)

            if args.list:
                return 0

            target = args.board
            if target is None:
                available = boards.get("boards") or []
                if not available:
                    print("error: this account has no boards to record", file=sys.stderr)
                    return 1
                # Prefer a board that has rows. Recording an empty board produces
                # a fixture that validates nothing about the item envelope, which
                # is most of what these recordings are for.
                target = available[0]["id"]
                for board in available:
                    probe = client.execute(
                        BOARD_ITEMS_FIRST, {"boardIds": [str(board["id"])], "limit": 1}
                    )
                    page = (probe["boards"][0] or {}).get("items_page") or {}
                    if page.get("items"):
                        target = board["id"]
                        break
                print(f"\nno --board given; using {target}")

            board_id = str(target)
            if not board_id.isdigit():
                match = [
                    board
                    for board in boards.get("boards", [])
                    if str(board["name"]).casefold() == board_id.casefold()
                ]
                if not match:
                    print(f"error: no board named {target!r}", file=sys.stderr)
                    return 1
                board_id = str(match[0]["id"])

            print(f"\nrecording BOARD_COLUMNS for {board_id} ...")
            columns = client.execute(BOARD_COLUMNS, {"boardIds": [board_id]})
            write("board_columns.json", columns, secrets)

            print(f"recording BOARD_ITEMS_FIRST for {board_id} ...")
            first = client.execute(
                BOARD_ITEMS_FIRST, {"boardIds": [board_id], "limit": args.limit}
            )
            write("board_items_page1.json", first, secrets)

            complexity = first.get("complexity") or {}
            print(f"  complexity: before={complexity.get('before')} "
                  f"query={complexity.get('query')} after={complexity.get('after')}")

            page = (first.get("boards") or [{}])[0].get("items_page") or {}
            print(f"  items on page 1: {len(page.get('items') or [])}, "
                  f"cursor: {page.get('cursor')!r}")

            if page.get("cursor"):
                print("recording BOARD_ITEMS_NEXT ...")
                following = client.execute(
                    BOARD_ITEMS_NEXT,
                    {"cursor": page["cursor"], "limit": args.limit},
                )
                write("board_items_page2.json", following, secrets)
        except BIAgentError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            print(f"user-facing: {exc.user_message}", file=sys.stderr)
            return 1

    if args.compare:
        print("\n--- structural comparison against authored fixtures ---")
        for name in COMPARISONS:
            compare(name)

    print("\nDone. Review tests/fixtures/live/ before committing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
