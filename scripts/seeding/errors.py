"""Seeding-specific errors, descending from F01's hierarchy.

Defined here rather than in `bi_agent/errors.py` for the same reason the whole
package lives under `scripts/`: seeding is not part of the shipped agent, and its
failure modes are a developer's problem, not a founder's. Adding them to F01
would put "the workbook header row was wrong" into the same namespace as errors
the chat UI renders.

They still descend from :class:`~bi_agent.errors.BIAgentError` so that a caller
catching that base class catches everything this repository raises deliberately.
"""

from __future__ import annotations

from bi_agent.errors import BIAgentError

__all__ = [
    "SeedError",
    "VerificationError",
    "WorkbookError",
    "WriteGateError",
]


class SeedError(BIAgentError):
    """Any failure while seeding monday.com from the workbooks."""

    default_user_message = (
        "Seeding the monday.com boards failed. No partial board should be used "
        "to answer questions until seeding completes and verifies."
    )


class WriteGateError(SeedError):
    """A document was sent through the write path that should not have been.

    The mirror image of :class:`~bi_agent.errors.ReadOnlyViolationError`: that one
    stops a write escaping the agent, this one stops anything *other* than a
    reviewed write escaping the seeder.
    """

    default_user_message = (
        "Blocked: the seeder refused a GraphQL document that is not one of its "
        "reviewed write operations. Nothing was sent."
    )


class WorkbookError(SeedError):
    """A source workbook is not shaped the way the data profile says it is."""

    default_user_message = (
        "A source workbook could not be read as expected, so seeding stopped "
        "before writing anything."
    )


class VerificationError(SeedError):
    """The seeded board does not match the workbook.

    Raised *after* writing, and deliberately fatal: a board holding 300 of 346
    deals answers every question confidently and wrongly, which is worse than a
    board that is obviously absent.
    """

    default_user_message = (
        "The seeded boards do not match the source workbooks. The data on "
        "monday.com should not be trusted until this is resolved."
    )
