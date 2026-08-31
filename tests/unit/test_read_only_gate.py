"""Cases 1-12: the read-only gate, tested adversarially.

FR-5 is a hard constraint enforced entirely in our code - the token is a personal
admin credential with full write access (plan section 9.2), so nothing below this
layer will stop a write. A gate that only catches the obvious spelling is theatre,
so this module attacks it from both sides: every plausible disguise for a write
operation must be rejected, and every legitimate query that merely *mentions* the
forbidden word must be accepted. A gate with false positives gets disabled by the
next developer, which is its own kind of failure.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bi_agent.errors import ReadOnlyViolationError
from bi_agent.monday import queries
from bi_agent.monday.queries import (
    REGISTRY,
    QueryDocument,
    sanitize,
    verify_read_only,
)

PACKAGE_ROOT = Path(queries.__file__).parent.parent

#: The word the gate exists to stop. Spelled out here, in the test suite, so it
#: never has to be spelled out in `bi_agent/` - which is what lets case 12 assert
#: its total absence from the package.
FORBIDDEN = "mutation"


# --- case 1: the registry itself ---------------------------------------------


def test_registry_is_populated_and_every_document_verifies():
    """Case 1: every shipped document passes the gate, and there are some."""
    assert REGISTRY, "the registry must not be empty"
    assert set(REGISTRY) == {
        "ME",
        "LIST_BOARDS",
        "BOARD_COLUMNS",
        "BOARD_ITEMS_FIRST",
        "BOARD_ITEMS_NEXT",
    }
    for name, document in REGISTRY.items():
        verify_read_only(document.text, name=name)
        assert queries._top_level_keywords(document.text) == ["query"], (
            f"{name} should be exactly one read operation"
        )


# --- cases 2-6, 10: writes must be rejected ----------------------------------

REJECTED = {
    # case 2: the obvious one
    "plain": FORBIDDEN + " { create_item(board_id: 1) { id } }",
    # case 3: case must not be an escape
    "upper": FORBIDDEN.upper() + " { create_item { id } }",
    "mixed": "MuTaTiOn { create_item { id } }",
    # case 4: leading whitespace and newlines
    "leading_whitespace": "\n\n\t   " + FORBIDDEN + " { create_item { id } }",
    # case 5: a named operation
    "named": FORBIDDEN + " CreateThing { create_item { id } }",
    # case 6: a subscription is not a read either
    "subscription": "subscription { events { id } }",
    # case 10: hiding in the second operation of a multi-operation document
    "second_operation": (
        "query Legit { me { id } } " + FORBIDDEN + " Sneaky { delete_item { id } }"
    ),
    # and the same trick after a variable-definition list, which is where a
    # brace/paren depth bug would show up
    "second_operation_after_variables": (
        "query Legit($limit: Int!) { boards(limit: $limit) { id } } "
        + FORBIDDEN
        + " Sneaky { delete_item { id } }"
    ),
    # a named operation carrying variables of its own
    "named_with_variables": (
        FORBIDDEN + " Create($boardId: ID!) { create_item(board_id: $boardId) { id } }"
    ),
}


@pytest.mark.parametrize("document", REJECTED.values(), ids=list(REJECTED))
def test_write_operations_are_rejected(document: str):
    """Cases 2-6 and 10: anything that is not a read raises."""
    with pytest.raises(ReadOnlyViolationError):
        verify_read_only(document)


@pytest.mark.parametrize("document", REJECTED.values(), ids=list(REJECTED))
def test_rejection_also_happens_at_construction(document: str):
    """The gate runs in ``__post_init__``, so a bad constant fails at import."""
    with pytest.raises(ReadOnlyViolationError):
        QueryDocument("BAD", document)


def test_rejection_message_is_specific_and_actionable():
    with pytest.raises(ReadOnlyViolationError) as excinfo:
        verify_read_only(FORBIDDEN + " { create_item { id } }", name="probe")
    message = str(excinfo.value)
    assert "probe" in message
    assert "read-only" in message
    assert "not sent" in message
    # The user-facing half comes from F01 and must not leak internals.
    assert "read-only" in excinfo.value.user_message


def test_empty_or_structureless_document_is_rejected():
    """No operation at all is not a read; refuse rather than send nothing."""
    for document in ("", "   \n  ", "# only a comment\n"):
        with pytest.raises(ReadOnlyViolationError):
            verify_read_only(document)


# --- cases 7-9: legitimate queries must NOT be rejected ----------------------

ACCEPTED = {
    # case 7: the word inside a comment
    "in_comment": (
        "# a " + FORBIDDEN + " would be blocked here\nquery Q { me { id } }"
    ),
    "in_trailing_comment": (
        "query Q { me { id } } # " + FORBIDDEN + " { create_item { id } }"
    ),
    # case 8: the word inside a string literal argument
    "in_string_argument": (
        'query Q { items_page(query_params: {rules: [{column_id: "name", '
        'compare_value: ["' + FORBIDDEN + '"]}]}) { items { id } } }'
    ),
    "in_block_string": (
        'query Q { items(filter: """' + FORBIDDEN + ' { x }""") { id } }'
    ),
    "in_escaped_string": (
        'query Q { items(filter: "say \\" ' + FORBIDDEN + '") { id } }'
    ),
    # case 9: the word as a substring of a field name
    "as_field_substring": "query Q { board { " + FORBIDDEN + "Count } }",
    "as_field_prefix": "query Q { board { total" + FORBIDDEN + "s } }",
    # a field alias sharing the name is still just a field
    "as_field_alias": "query Q { board { " + FORBIDDEN + ": name } }",
    # the anonymous shorthand form is a query
    "shorthand": "{ me { id name is_admin } }",
    # fragments are selection sets, not operations
    "with_fragment": "fragment F on Item { id name } query Q { items { ...F } }",
}


@pytest.mark.parametrize("document", ACCEPTED.values(), ids=list(ACCEPTED))
def test_legitimate_queries_are_accepted(document: str):
    """Cases 7-9: no false positives. Mentioning the word is not performing it."""
    verify_read_only(document)
    QueryDocument("OK", document)


def test_sanitize_removes_comments_and_string_contents():
    """The mechanism behind cases 7-9, asserted directly."""
    assert FORBIDDEN not in sanitize("# " + FORBIDDEN + "\nquery Q { id }")
    assert FORBIDDEN not in sanitize('query Q { f(a: "' + FORBIDDEN + '") }')
    assert FORBIDDEN not in sanitize('query Q { f(a: """' + FORBIDDEN + '""") }')
    # Structure survives, so the depth tracking still works afterwards.
    assert sanitize("query Q { id }").count("{") == 1


def test_sanitize_survives_an_unterminated_string():
    """Malformed input must not hang or crash - it just must not be accepted."""
    sanitize('query Q { f(a: "unterminated')
    sanitize('query Q { f(a: """unterminated')


# --- case 11: a bare string cannot reach the transport -----------------------


def test_execute_refuses_a_bare_string(monday_client_factory):
    """Case 11: rejected before any HTTP call - the router is never touched."""
    from bi_agent.monday.client import MondayClient

    client, route = monday_client_factory()
    assert isinstance(client, MondayClient)

    with pytest.raises(ReadOnlyViolationError):
        client.execute("query Q { me { id } }")  # type: ignore[arg-type]

    with pytest.raises(ReadOnlyViolationError):
        client.execute(FORBIDDEN + " { create_item { id } }")  # type: ignore[arg-type]

    assert route.call_count == 0, "no request may leave the process"


# --- case 12: the suite-wide sweep -------------------------------------------


def _string_literals(tree: ast.AST) -> list[str]:
    """Every string constant in a module except its docstrings.

    Docstrings are excluded on purpose: prose has to be able to *discuss* the
    thing being prevented. A GraphQL document, by contrast, is necessarily a
    string constant in executable position, which is exactly what this catches.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_write_operation_literal_anywhere_in_the_package():
    """Case 12: guards against a future addition bypassing the registry.

    The gate protects the documents in `queries.py`. This protects against a
    document that never goes through the gate at all - a write assembled as a
    string somewhere else in the package. Because the verifier is a keyword
    whitelist, `bi_agent/` never needs the forbidden word in executable code, so
    its presence in any string literal is by definition something new.
    """
    modules = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert modules, "the sweep found no modules to scan"

    offenders: list[str] = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for literal in _string_literals(tree):
            if FORBIDDEN in literal.casefold():
                offenders.append(f"{path.relative_to(PACKAGE_ROOT.parent)}: {literal!r}")

    assert not offenders, (
        "a write operation may have been introduced outside the registry:\n"
        + "\n".join(offenders)
    )


def test_sweep_would_catch_a_planted_literal(tmp_path: Path):
    """The sweep is only worth having if it actually fails on a violation."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        '"""A docstring mentioning ' + FORBIDDEN + ' is fine."""\n'
        "DOC = '" + FORBIDDEN + " { create_item { id } }'\n",
        encoding="utf-8",
    )
    literals = _string_literals(ast.parse(planted.read_text(encoding="utf-8")))
    assert any(FORBIDDEN in literal for literal in literals)
    assert not any(literal.startswith("A docstring") for literal in literals)
