"""Approval-queue CRUD helpers.

Every interaction with ``approval_items`` should go through this
module so the legal state transitions stay enforced in one place.

Categories
----------

``category`` is opaque to the table but each value has a
documented ``payload`` shape:

- ``email_draft``: ``{"to": [...], "cc": [...], "subject": str,
  "body_html": str, "in_reply_to_message_id": str | None}``.
  Produced by Smart Responder; consumed by Phase 9-4 send-on-
  approve.
- ``client_interaction``: ``{"client_name": str, "subject": str,
  "summary": str, "occurred_at": iso, ...}``. Produced by
  correspondence_logger; consumed by the memory writer once
  approved.
- ``entity_change``: ``{"entity_type": str, "name": str,
  "fields": {...}, "rationale": str}``. Produced by knowledge-
  graph plugins; consumed by the KG writer.

New categories don't need a migration — the JSONB payload is
schema-free at the DB layer. Per-category validation is the
caller's responsibility (typically a Pydantic model).
"""
import datetime as _dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db.models import ApprovalItem


class ApprovalTransitionError(Exception):
    """Raised when a state transition isn't legal.

    Today: any attempt to approve/reject something that isn't
    currently ``pending`` raises this. The application catches
    and surfaces 409 Conflict to the principal; tests assert
    on it directly.
    """


@dataclass
class CreateApprovalInput:
    """Inputs the caller controls; everything else is set by the
    helper (id, status='pending', timestamps).
    """

    plugin_name: str
    category: str
    summary: str
    payload: dict[str, Any]
    trace_id: uuid.UUID | None = None


async def create_approval(
    session: AsyncSession,
    firm_id: uuid.UUID,
    *,
    input: CreateApprovalInput,
) -> ApprovalItem:
    """Insert a new ``pending`` row. Caller commits.

    ``session`` must already be inside ``firm_context(firm_id)``;
    RLS rejects the INSERT otherwise.
    """
    row = ApprovalItem(
        firm_id=firm_id,
        trace_id=input.trace_id,
        plugin_name=input.plugin_name,
        category=input.category,
        summary=input.summary,
        payload=input.payload,
        status="pending",
    )
    session.add(row)
    await session.flush()
    return row


async def list_pending(
    session: AsyncSession,
    firm_id: uuid.UUID,
    *,
    limit: int = 50,
) -> Sequence[ApprovalItem]:
    """Most-recent-first list of ``pending`` items for one firm.

    The schema's partial index on ``(firm_id, created_at DESC)
    WHERE status='pending'`` matches this query exactly.
    """
    result = await session.execute(
        select(ApprovalItem)
        .where(ApprovalItem.firm_id == firm_id)
        .where(ApprovalItem.status == "pending")
        .order_by(ApprovalItem.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_by_id(
    session: AsyncSession,
    item_id: uuid.UUID,
) -> ApprovalItem | None:
    """RLS-scoped lookup; returns None for cross-firm or missing ids."""
    return (
        await session.execute(
            select(ApprovalItem).where(ApprovalItem.id == item_id)
        )
    ).scalar_one_or_none()


async def approve(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    decided_by_user_id: uuid.UUID,
    notes: str | None = None,
    now: _dt.datetime | None = None,
) -> ApprovalItem:
    """Transition ``pending`` -> ``approved``.

    Raises ApprovalTransitionError if the row isn't pending,
    LookupError if the row doesn't exist (or RLS hides it).
    """
    return await _decide(
        session,
        item_id,
        new_status="approved",
        decided_by_user_id=decided_by_user_id,
        notes=notes,
        now=now,
    )


async def reject(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    decided_by_user_id: uuid.UUID,
    notes: str | None = None,
    now: _dt.datetime | None = None,
) -> ApprovalItem:
    """Transition ``pending`` -> ``rejected``."""
    return await _decide(
        session,
        item_id,
        new_status="rejected",
        decided_by_user_id=decided_by_user_id,
        notes=notes,
        now=now,
    )


async def _decide(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    new_status: str,
    decided_by_user_id: uuid.UUID,
    notes: str | None,
    now: _dt.datetime | None,
) -> ApprovalItem:
    row = await get_by_id(session, item_id)
    if row is None:
        raise LookupError(f"approval item {item_id} not found")
    if row.status != "pending":
        raise ApprovalTransitionError(
            f"approval item {item_id} is {row.status!r}; cannot transition "
            f"to {new_status!r}"
        )
    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    row.status = new_status
    row.decided_at = now
    row.decided_by_user_id = decided_by_user_id
    row.decision_notes = notes
    row.updated_at = now
    await session.flush()
    return row
