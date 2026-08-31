"""monday.com access: authenticated, paginated, cached, and structurally read-only.

Everything above this package says "give me the Deals board" and receives rows.
What it does *not* receive is canonical field names — a :class:`BoardSnapshot`
carries monday.com's own payload shape, and F04 owns the translation.

Typical use::

    with MondayClient(settings) as client:
        reader = BoardReader(client)
        snapshot = reader.fetch_items("Deals")
        if snapshot.is_stale:
            ...  # tell the user how old `snapshot.fetched_at` is

The read-only guarantee lives in `queries.py`: the transport accepts only a
verified :class:`QueryDocument`, and every one of them is built and checked at
import time. See that module for why the guarantee is structural rather than a
matter of care.
"""

from bi_agent.monday.boards import BoardReader, BoardRef, BoardSnapshot, Column
from bi_agent.monday.client import MondayClient
from bi_agent.monday.queries import REGISTRY, QueryDocument, verify_read_only

__all__ = [
    "REGISTRY",
    "BoardReader",
    "BoardRef",
    "BoardSnapshot",
    "Column",
    "MondayClient",
    "QueryDocument",
    "verify_read_only",
]
