"""The frozen registry of GraphQL write documents this seeder may send.

Mirrors `bi_agent/monday/queries.py` in the inverse direction (F03 section
3.2). That module's gate refuses anything that is not a read; this one
refuses anything that is not a single, reviewed write:

**Mechanism 1 - the registry itself.** `SeedWriter.execute` (writer.py)
accepts only a `MutationDocument`, and every `MutationDocument` in the process
is constructed in this module at import time. There is no code path that
sends a caller-built string.

**Mechanism 2 - lexical verification.** `verify_write_only` runs inside
`MutationDocument.__post_init__`, so a document that is not exactly one
`mutation` operation fails at *import* time, taking the test suite down with
it, rather than failing mid-run against a live board.

`_top_level_keywords` is a structural copy of the private helper in
`bi_agent.monday.queries` (same tokenizer, same reasoning about depth and
comments/strings) — duplicated rather than imported so this module stays a
single file a reviewer can read end to end to answer "which writes can this
repository perform", per section 3.2's whole argument for keeping the write
path self-contained. `sanitize` itself *is* imported: it is public, and
re-implementing comment/string stripping would be duplication with no
review benefit.
"""

from __future__ import annotations

from dataclasses import dataclass

from bi_agent.monday.queries import sanitize
from scripts.seeding.errors import WriteGateError

__all__ = [
    "CREATE_BOARD",
    "CREATE_COLUMN",
    "CREATE_ITEM",
    "DELETE_BOARD",
    "DELETE_COLUMN",
    "DELETE_ITEM",
    "MUTATION_REGISTRY",
    "MutationDocument",
    "verify_write_only",
]

_IDENTIFIER_START = set("_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_IDENTIFIER_BODY = _IDENTIFIER_START | set("0123456789")


def _top_level_keywords(document: str) -> list[str]:
    """The keyword opening each top-level operation, in order. See queries.py."""
    text = sanitize(document)
    keywords: list[str] = []
    depth = 0
    expecting_operation = True
    i, end = 0, len(text)

    while i < end:
        char = text[i]
        if char in "{(":
            if depth == 0 and expecting_operation and char == "{":
                keywords.append("query")
                expecting_operation = False
            depth += 1
        elif char in "})":
            depth -= 1
            if depth == 0 and char == "}":
                expecting_operation = True
        elif depth == 0 and expecting_operation and char in _IDENTIFIER_START:
            start = i
            while i < end and text[i] in _IDENTIFIER_BODY:
                i += 1
            keywords.append(text[start:i])
            expecting_operation = False
            continue
        i += 1

    return keywords


def verify_write_only(document: str, *, name: str = "document") -> None:
    """Raise :class:`WriteGateError` unless `document` is exactly one mutation.

    Two ways to fail: not exactly one top-level operation (a bare read query
    handed to the writer opens one operation too, so count alone is not
    enough), or that one operation is not spelled `mutation`.
    """
    keywords = _top_level_keywords(document)

    if len(keywords) != 1:
        raise WriteGateError(
            f"GraphQL {name} opens {len(keywords)} top-level operation(s); "
            "a seeding write document must open exactly one. Nothing sent."
        )

    if keywords[0].casefold() != "mutation":
        raise WriteGateError(
            f"GraphQL {name} opens a top-level operation with "
            f"{keywords[0]!r}, not 'mutation'. Only a reviewed write may be "
            "sent through the seed writer. Nothing sent."
        )


@dataclass(frozen=True)
class MutationDocument:
    """A GraphQL document verified, at construction, to be one write operation.

    Frozen, so a verified document cannot be edited into an unverified one.
    `SeedWriter.execute` accepts only this type — a `QueryDocument` from
    `bi_agent.monday.queries` or a bare string is refused before anything is
    sent (write_gate tests 53-55).
    """

    name: str
    text: str

    def __post_init__(self) -> None:
        verify_write_only(self.text, name=f"document {self.name!r}")

    def __str__(self) -> str:
        return self.text


# --- the registry -------------------------------------------------------------
#
# Five documents, one write operation each. `create_item`'s `column_values` is
# sent as a JSON-encoded string, matching monday.com's `JSON` scalar as
# documented for `create_item` / `change_multiple_column_values`.

CREATE_BOARD = MutationDocument(
    "CREATE_BOARD",
    """
    mutation CreateBoard($name: String!, $kind: BoardKind!) {
      create_board(board_name: $name, board_kind: $kind) {
        id
        name
      }
    }
    """,
)

CREATE_COLUMN = MutationDocument(
    "CREATE_COLUMN",
    """
    mutation CreateColumn(
      $boardId: ID!
      $title: String!
      $columnType: ColumnType!
    ) {
      create_column(
        board_id: $boardId
        title: $title
        column_type: $columnType
      ) {
        id
        title
      }
    }
    """,
)

DELETE_COLUMN = MutationDocument(
    "DELETE_COLUMN",
    """
    mutation DeleteColumn($boardId: ID!, $columnId: String!) {
      delete_column(board_id: $boardId, column_id: $columnId) {
        id
      }
    }
    """,
)

CREATE_ITEM = MutationDocument(
    "CREATE_ITEM",
    """
    mutation CreateItem(
      $boardId: ID!
      $itemName: String!
      $columnValues: JSON!
    ) {
      create_item(
        board_id: $boardId
        item_name: $itemName
        column_values: $columnValues
      ) {
        id
        name
      }
    }
    """,
)

#: A freshly created board carries one auto-generated sample item (observed
#: on the first live run: named "Task 1", no columns filled in) that is not
#: part of any workbook row and is invisible to the `columns` query that
#: default-column cleanup uses. Sent only against items on a board this
#: seeder just created, before any of its own items exist, in
#: `resolve_or_create_board`.
DELETE_ITEM = MutationDocument(
    "DELETE_ITEM",
    """
    mutation DeleteItem($itemId: ID!) {
      delete_item(item_id: $itemId) {
        id
      }
    }
    """,
)

#: The one destructive document. It is sent from exactly one place in
#: `scripts/seed_monday.py` — the `--recreate` path, gated behind a
#: confirmation prompt that names the board and its item count (F03 section
#: 3.6). `test_write_gate.py` case 57 asserts it appears nowhere else.
DELETE_BOARD = MutationDocument(
    "DELETE_BOARD",
    """
    mutation DeleteBoard($boardId: ID!) {
      delete_board(board_id: $boardId) {
        id
      }
    }
    """,
)

#: Every write document in the process, by name. A document constructed but
#: left out of this registry is a gap the tests can catch by iterating it.
MUTATION_REGISTRY: dict[str, MutationDocument] = {
    document.name: document
    for document in (
        CREATE_BOARD,
        CREATE_COLUMN,
        DELETE_COLUMN,
        CREATE_ITEM,
        DELETE_ITEM,
        DELETE_BOARD,
    )
}
