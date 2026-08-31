"""Tests for the seeding write gate — the inverse of bi_agent's read-only gate
(bi_agent/monday/queries.py, tests/unit/test_read_only_gate.py).

That gate refuses anything that is not a read; this one refuses anything that
is not a single, reviewed write (F03 section 3.2).
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from bi_agent.monday.queries import LIST_BOARDS
from scripts.seeding.errors import WriteGateError
from scripts.seeding.mutations import (
    MUTATION_REGISTRY,
    verify_write_only,
)
from scripts.seeding.writer import SeedWriter

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_registry_is_non_empty():
    assert MUTATION_REGISTRY


@pytest.mark.parametrize("document", list(MUTATION_REGISTRY.values()), ids=lambda d: d.name)
def test_every_registered_document_opens_exactly_one_write(document):
    """Case 53: passes its guard; construction already proved this, re-run explicitly."""
    verify_write_only(document.text, name=document.name)  # must not raise


def test_a_read_document_is_rejected_by_the_write_gate():
    """Case 54: the guard is two-way — a misfiled read constant fails loudly."""
    with pytest.raises(WriteGateError):
        verify_write_only(LIST_BOARDS.text, name="LIST_BOARDS")


def test_seed_writer_execute_refuses_a_read_query_document(settings_factory):
    writer = SeedWriter(settings_factory(), http_client=httpx.Client())
    try:
        with pytest.raises(WriteGateError):
            writer.execute(LIST_BOARDS)
    finally:
        writer.close()


def test_seed_writer_execute_refuses_a_bare_string(settings_factory):
    """Case 55: rejected before any HTTP call — no respx route is registered."""
    writer = SeedWriter(settings_factory(), http_client=httpx.Client())
    try:
        with pytest.raises(WriteGateError):
            writer.execute('mutation { create_board(board_name: "x") { id } }')
    finally:
        writer.close()


def test_a_document_opening_two_operations_is_rejected():
    with pytest.raises(WriteGateError):
        verify_write_only(
            "mutation A { create_board(board_name: \"x\") { id } } "
            "mutation B { create_board(board_name: \"y\") { id } }",
            name="double",
        )


def test_bi_agent_never_imports_scripts():
    """Case 56: the structural half of FR-5 — an AST sweep, not a convention."""
    bi_agent_dir = REPO_ROOT / "bi_agent"
    offenders: list[str] = []

    for path in bi_agent_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "scripts" or name.startswith("scripts.") for name in names):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, f"bi_agent imports scripts in: {offenders}"


def test_delete_board_is_sent_from_exactly_one_call_site():
    """Case 57: the destructive document exists in exactly one code path."""
    seed_script = (REPO_ROOT / "scripts" / "seed_monday.py").read_text(encoding="utf-8")
    assert seed_script.count("writer.execute(DELETE_BOARD") == 1
