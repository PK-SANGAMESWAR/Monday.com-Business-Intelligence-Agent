"""The frozen registry of GraphQL documents this agent is allowed to send.

FR-5 says the agent never modifies a board, and plan section 9.2 established that
the credential offers no help: the supplied token is a personal *admin* token with
full write permissions. So the guarantee has to live in our code, and it lives
here, in two independent mechanisms:

**Mechanism 1 - the registry itself.** :meth:`MondayClient.execute` accepts only a
:class:`QueryDocument`, and every ``QueryDocument`` in the process is constructed
in this module at import time. There is no code path that sends a caller-supplied
string, so "the agent decided to write" is not expressible - not merely blocked.

**Mechanism 2 - lexical verification.** :func:`verify_read_only` runs inside
``QueryDocument.__post_init__``, so a write operation added to this file fails at
**import** time and takes the whole test suite down with it, rather than failing
in a user's session.

The verifier works on a *whitelist* of permitted top-level keywords rather than a
blacklist of forbidden ones. That is deliberate and load-bearing: a blacklist has
to enumerate every way to spell a write operation, whereas a whitelist rejects
everything it does not recognise, including spellings nobody thought of. It also
means the forbidden keyword never appears as a literal in this package, so the
suite-wide sweep in ``test_read_only_gate.py`` can assert its total absence.

Considered and rejected: validating with ``graphql-core``. More rigorous than a
lexical check, but a whole new dependency whose entire value here is checking five
hand-written constants that a human reviews, when mechanism 1 has already made
mechanism 2 defence in depth. Recorded so the trade-off is deliberate; if this
document set ever grows or becomes dynamic, revisit it.
"""

from __future__ import annotations

from dataclasses import dataclass

from bi_agent.errors import ReadOnlyViolationError

__all__ = [
    "BOARD_COLUMNS",
    "BOARD_ITEMS_FIRST",
    "BOARD_ITEMS_NEXT",
    "DEFAULT_PAGE_SIZE",
    "LIST_BOARDS",
    "ME",
    "PERMITTED_TOP_LEVEL_KEYWORDS",
    "REGISTRY",
    "QueryDocument",
    "sanitize",
    "verify_read_only",
]

#: The only top-level keywords a document may open an operation with. ``query``
#: is the read operation; ``fragment`` is a reusable selection set, not an
#: operation at all, and cannot write. Anything else is refused by name.
PERMITTED_TOP_LEVEL_KEYWORDS = frozenset({"query", "fragment"})

#: Items per page. monday.com caps ``items_page`` at 500, and both our boards fit
#: in a single page at this size - pagination is exercised by tests regardless,
#: because "it fits today" is not a design.
DEFAULT_PAGE_SIZE = 500

_IDENTIFIER_START = set("_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_IDENTIFIER_BODY = _IDENTIFIER_START | set("0123456789")


def sanitize(document: str) -> str:
    """Strip comments and string *contents*, leaving structure intact.

    This is what stops the three false-positive cases in the test plan (a
    forbidden keyword inside a ``#`` comment, inside a string argument, inside a
    block string) from being mistaken for an operation, and equally stops a real
    operation from hiding behind a quote. Each removed region becomes a quoted
    empty string surrounded by spaces, so tokens on either side stay separated.
    """
    out: list[str] = []
    i, end = 0, len(document)

    while i < end:
        char = document[i]

        if char == "#":
            while i < end and document[i] != "\n":
                i += 1
            out.append(" ")
            continue

        if document.startswith('"""', i):
            close = document.find('"""', i + 3)
            i = end if close == -1 else close + 3
            out.append(' "" ')
            continue

        if char == '"':
            i += 1
            while i < end and document[i] != '"':
                # A backslash escapes the next character, so an escaped quote
                # does not end the string.
                i += 2 if document[i] == "\\" else 1
            i += 1
            out.append(' "" ')
            continue

        out.append(char)
        i += 1

    return "".join(out)


def _top_level_keywords(document: str) -> list[str]:
    """The keyword opening each top-level operation, in order.

    Only the *first* token of each operation is returned. Everything nested
    inside braces or parentheses is skipped, which is what makes a field named
    ``mutationCount`` or a variable definition like ``($boardId: ID!)``
    structurally incapable of tripping the gate: neither sits at depth zero at
    the start of an operation.

    The anonymous shorthand form ``{ me { id } }`` yields the synthetic keyword
    ``"query"``, because that is exactly what it is.
    """
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
            # Back at the top level after a *selection set*: whatever comes next
            # opens a new operation. A closing paren does not qualify - it ends a
            # variable-definition list, and the operation it belongs to has
            # already been counted.
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


def verify_read_only(document: str, *, name: str = "document") -> None:
    """Raise :class:`ReadOnlyViolationError` unless every operation is a read.

    Case is folded before comparison. GraphQL keywords are case-sensitive, so a
    capitalised spelling would be rejected by the server anyway - but a gate that
    lets it through has to be reasoned about, and one that does not, does not.
    """
    keywords = _top_level_keywords(document)

    if not keywords:
        raise ReadOnlyViolationError(
            f"GraphQL {name} contains no operation; refusing to send it. "
            "Only read queries from the registry may be sent."
        )

    for keyword in keywords:
        if keyword.casefold() not in PERMITTED_TOP_LEVEL_KEYWORDS:
            permitted = ", ".join(sorted(PERMITTED_TOP_LEVEL_KEYWORDS))
            raise ReadOnlyViolationError(
                f"GraphQL {name} opens a top-level operation with {keyword!r}, "
                f"which is not a read. Permitted keywords: {permitted}. "
                "This agent is read-only, so the request was not sent."
            )


@dataclass(frozen=True)
class QueryDocument:
    """A GraphQL document that has been verified as read-only.

    Frozen, so a verified document cannot be edited into an unverified one after
    the fact. Its existence is the proof the gate ran: the transport accepts this
    type and nothing else, so a bare string cannot reach the network.
    """

    name: str
    text: str

    def __post_init__(self) -> None:
        verify_read_only(self.text, name=f"document {self.name!r}")

    def __str__(self) -> str:
        return self.text


# --- the registry -------------------------------------------------------------
#
# Five documents. Item fields deliberately request both `text` and `value`:
# `text` is the display string a user would see in the board UI, `value` is the
# raw JSON that preserves the type information F04 needs for dates and numbers.
# Fetching one and wanting the other later would mean re-recording every fixture.

ME = QueryDocument(
    "ME",
    """
    query Me {
      me { id name is_admin }
    }
    """,
)

LIST_BOARDS = QueryDocument(
    "LIST_BOARDS",
    """
    query ListBoards($limit: Int!) {
      boards(limit: $limit, state: active, order_by: created_at) {
        id
        name
      }
    }
    """,
)

BOARD_COLUMNS = QueryDocument(
    "BOARD_COLUMNS",
    """
    query BoardColumns($boardIds: [ID!]) {
      boards(ids: $boardIds) {
        id
        name
        columns { id title type }
      }
    }
    """,
)

# One request returns the board's identity, its column definitions and the first
# page of items. `complexity` rides along so spend is logged (NFR-7); the budget
# is ~990k points against a cost in the hundreds, so this is observability, not a
# constraint we expect to bind.
BOARD_ITEMS_FIRST = QueryDocument(
    "BOARD_ITEMS_FIRST",
    """
    query BoardItemsFirst($boardIds: [ID!], $limit: Int!) {
      complexity { before after query }
      boards(ids: $boardIds) {
        id
        name
        columns { id title type }
        items_page(limit: $limit) {
          cursor
          items {
            id
            name
            column_values { id type text value }
          }
        }
      }
    }
    """,
)

BOARD_ITEMS_NEXT = QueryDocument(
    "BOARD_ITEMS_NEXT",
    """
    query BoardItemsNext($cursor: String!, $limit: Int!) {
      complexity { before after query }
      next_items_page(limit: $limit, cursor: $cursor) {
        cursor
        items {
          id
          name
          column_values { id type text value }
        }
      }
    }
    """,
)

#: Every document in the process, by name. The read-only sweep iterates this, so
#: a document added to the module but left out of the registry is a test failure.
REGISTRY: dict[str, QueryDocument] = {
    document.name: document
    for document in (
        ME,
        LIST_BOARDS,
        BOARD_COLUMNS,
        BOARD_ITEMS_FIRST,
        BOARD_ITEMS_NEXT,
    )
}
