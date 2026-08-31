"""Registry integrity: the documents are the API contract, so pin them.

These are cheap assertions about content that other layers silently depend on.
If `column_values` stops requesting `value`, F04 loses every date and number and
finds out three features later; asserting it here makes the break local.
"""

from __future__ import annotations

import dataclasses

import pytest

from bi_agent.monday import queries
from bi_agent.monday.queries import (
    BOARD_COLUMNS,
    BOARD_ITEMS_FIRST,
    BOARD_ITEMS_NEXT,
    DEFAULT_PAGE_SIZE,
    LIST_BOARDS,
    ME,
    REGISTRY,
    QueryDocument,
)

ITEM_DOCUMENTS = (BOARD_ITEMS_FIRST, BOARD_ITEMS_NEXT)


def test_registry_keys_match_document_names():
    for name, document in REGISTRY.items():
        assert document.name == name


def test_every_module_level_document_is_registered():
    """A document defined but left out of the registry escapes the sweep."""
    defined = {
        value
        for value in vars(queries).values()
        if isinstance(value, QueryDocument)
    }
    assert defined == set(REGISTRY.values())


def test_documents_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ME.text = "query Q { me { id } }"  # type: ignore[misc]


def test_me_document_probes_identity_and_admin_status():
    """`is_admin` is requested because plan section 9.2 turns on it being true."""
    for field in ("me", "id", "name", "is_admin"):
        assert field in ME.text


def test_list_boards_requests_id_and_name_only():
    """Board resolution needs nothing else, and items here would be expensive."""
    assert "boards(" in LIST_BOARDS.text
    assert "state: active" in LIST_BOARDS.text
    assert "items_page" not in LIST_BOARDS.text


def test_board_columns_requests_the_title_to_id_mapping():
    for field in ("columns", "id", "title", "type"):
        assert field in BOARD_COLUMNS.text


@pytest.mark.parametrize("document", ITEM_DOCUMENTS, ids=lambda d: d.name)
def test_item_documents_request_both_text_and_value(document: QueryDocument):
    """NFR-8 and F04's needs: display string *and* typed raw JSON."""
    assert "column_values { id type text value }" in document.text
    assert "cursor" in document.text


@pytest.mark.parametrize("document", ITEM_DOCUMENTS, ids=lambda d: d.name)
def test_item_documents_report_complexity(document: QueryDocument):
    """NFR-7: spend is observable per request, not inferred."""
    assert "complexity { before after query }" in document.text


def test_first_page_document_bundles_board_identity_and_columns():
    """One request for name, columns and page one keeps the fetch to 2-4 calls."""
    assert "items_page(limit: $limit)" in BOARD_ITEMS_FIRST.text
    assert "columns { id title type }" in BOARD_ITEMS_FIRST.text
    assert "next_items_page" not in BOARD_ITEMS_FIRST.text


def test_next_page_document_takes_a_cursor():
    assert "next_items_page(limit: $limit, cursor: $cursor)" in BOARD_ITEMS_NEXT.text
    # The cursor already encodes the board, so re-sending an id would be wrong.
    assert "boards(ids:" not in BOARD_ITEMS_NEXT.text


def test_page_size_is_within_the_api_cap():
    """monday.com rejects `items_page` limits above 500."""
    assert 0 < DEFAULT_PAGE_SIZE <= 500


def test_str_of_a_document_is_its_text():
    assert str(ME) == ME.text
