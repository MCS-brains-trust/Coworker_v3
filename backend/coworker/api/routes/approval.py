"""HTTP surface for the approval queue.

The Phase 10 web frontend calls these routes. Every route requires
``current_user`` (a valid session cookie) and operates inside
``firm_context(user.firm_id)`` so RLS scopes the queries to one
firm.

Routes (all under /api/v1/approvals):
    GET  /api/v1/approvals/pending           list the firm's pending items
    GET  /api/v1/approvals/{item_id}         fetch one item
    POST /api/v1/approvals/{item_id}/approve pending -> approved
    POST /api/v1/approvals/{item_id}/reject  pending -> rejected
    PUT  /api/v1/approvals/{item_id}/payload edit pending item payload

Approve / reject return the post-transition row so the client
doesn't need a follow-up GET. ``409 Conflict`` covers
ApprovalTransitionError; ``404 Not Found`` covers both unknown
ids and cross-firm reads (RLS hides them).
"""
import datetime as _dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.api.deps import current_user
from coworker.approval.items import (
    ApprovalTransitionError,
    approve,
    edit_payload,
    get_by_id,
    list_pending,
    reject,
)
from coworker.db.models import ApprovalItem, User
from coworker.db.session import firm_context, get_session

router = APIRouter(prefix="/api/v1/approvals", tags=["approval"])

# How many pending items the list endpoint returns per call. The
# table's partial index orders by (firm_id, created_at DESC) so
# pagination is cheap; deeper pages are future work (Phase 9-3).
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


class ApprovalItemResponse(BaseModel):
    """Outbound representation. Mirrors the row, omitting firm_id
    (the response is implicitly scoped to the authenticated firm).
    """

    id: uuid.UUID
    trace_id: uuid.UUID | None
    plugin_name: str
    category: str
    summary: str
    payload: dict[str, Any]
    status: str
    decided_at: _dt.datetime | None
    decided_by_user_id: uuid.UUID | None
    decision_notes: str | None
    last_edited_at: _dt.datetime | None
    last_edited_by_user_id: uuid.UUID | None
    required_approvals: int
    approval_signatures: list[dict[str, Any]]
    confidence: float | None
    created_at: _dt.datetime
    updated_at: _dt.datetime

    @classmethod
    def from_row(cls, row: ApprovalItem) -> "ApprovalItemResponse":
        return cls(
            id=row.id,
            trace_id=row.trace_id,
            plugin_name=row.plugin_name,
            category=row.category,
            summary=row.summary,
            payload=row.payload,
            status=row.status,
            decided_at=row.decided_at,
            decided_by_user_id=row.decided_by_user_id,
            decision_notes=row.decision_notes,
            last_edited_at=row.last_edited_at,
            last_edited_by_user_id=row.last_edited_by_user_id,
            required_approvals=row.required_approvals,
            approval_signatures=list(row.approval_signatures or []),
            confidence=row.confidence,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class DecisionBody(BaseModel):
    """Optional principal note attached to an approve / reject."""

    notes: str | None = Field(
        default=None, max_length=2000,
        description="Free-text rationale shown in the trace history.",
    )


class EditPayloadBody(BaseModel):
    """In-place edit of a pending item's payload.

    The client sends the complete updated payload — this isn't a
    JSON patch. Wholesale replacement avoids dropping fields the
    backend cares about that the client doesn't know about.
    """

    payload: dict[str, Any] = Field(
        description="Replacement payload. Shape depends on category.",
    )


@router.get("/pending", response_model=list[ApprovalItemResponse])
async def list_pending_route(
    limit: int = _DEFAULT_LIMIT,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalItemResponse]:
    """Return the firm's pending items, newest first."""
    if limit < 1 or limit > _MAX_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"limit must be between 1 and {_MAX_LIMIT}",
        )
    async with firm_context(user.firm_id):
        rows = await list_pending(session, user.firm_id, limit=limit)
    return [ApprovalItemResponse.from_row(r) for r in rows]


@router.get("/{item_id}", response_model=ApprovalItemResponse)
async def get_one(
    item_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalItemResponse:
    """Fetch one item. RLS hides cross-firm rows -> 404."""
    async with firm_context(user.firm_id):
        row = await get_by_id(session, item_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="approval item not found",
        )
    return ApprovalItemResponse.from_row(row)


@router.post(
    "/{item_id}/approve",
    response_model=ApprovalItemResponse,
)
async def approve_route(
    item_id: uuid.UUID,
    body: DecisionBody | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalItemResponse:
    """Transition a pending item to ``approved``."""
    notes = body.notes if body is not None else None
    async with firm_context(user.firm_id):
        try:
            row = await approve(
                session,
                item_id,
                decided_by_user_id=user.id,
                notes=notes,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval item not found",
            ) from exc
        except ApprovalTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        await session.commit()
    return ApprovalItemResponse.from_row(row)


@router.put(
    "/{item_id}/payload",
    response_model=ApprovalItemResponse,
)
async def edit_payload_route(
    item_id: uuid.UUID,
    body: EditPayloadBody,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalItemResponse:
    """Replace a pending item's payload (in-place edit).

    The item stays in ``pending``; only approve / reject move it
    to a terminal state. Editing a non-pending item returns
    ``409 Conflict``.
    """
    async with firm_context(user.firm_id):
        try:
            row = await edit_payload(
                session,
                item_id,
                new_payload=body.payload,
                edited_by_user_id=user.id,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval item not found",
            ) from exc
        except ApprovalTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        await session.commit()
    return ApprovalItemResponse.from_row(row)


@router.post(
    "/{item_id}/reject",
    response_model=ApprovalItemResponse,
)
async def reject_route(
    item_id: uuid.UUID,
    body: DecisionBody | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalItemResponse:
    """Transition a pending item to ``rejected``."""
    notes = body.notes if body is not None else None
    async with firm_context(user.firm_id):
        try:
            row = await reject(
                session,
                item_id,
                decided_by_user_id=user.id,
                notes=notes,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval item not found",
            ) from exc
        except ApprovalTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        await session.commit()
    return ApprovalItemResponse.from_row(row)
