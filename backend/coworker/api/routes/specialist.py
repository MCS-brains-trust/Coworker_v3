"""HTTP surface for the specialists registry and their versioned prompts.

The Phase 10 web frontend's Specialists page calls these routes. All
three require a session cookie (handled by ``current_user``); the PUT
additionally requires ``owner`` or ``principal`` (handled by
``require_role``). Every handler runs DB work inside
``firm_context(user.firm_id)`` so RLS scopes the queries to one firm.

Routes (all under /api/v1/specialists):
    GET /api/v1/specialists                       list the firm's specialists
    GET /api/v1/specialists/{id}/prompt           fetch the active prompt
    PUT /api/v1/specialists/{id}/prompt           update the active prompt

A PUT inserts a new ``specialist_prompt_versions`` row marked
``active`` and retires the prior active row in the same transaction.
The partial unique index on
``(specialist_id) WHERE status='active'`` keeps the invariant honest
even under concurrent PUTs: the second one will collide on the index
and fail rather than silently double-activate.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.api.deps import current_user, require_role
from coworker.api.schemas.specialist import (
    SpecialistListResponse,
    SpecialistPromptResponse,
    SpecialistPromptUpdate,
    SpecialistSummary,
)
from coworker.db.models.specialist import Specialist, SpecialistPromptVersion
from coworker.db.models.tenancy import User
from coworker.db.session import firm_context, get_session
from coworker.security.audit import append_audit

router = APIRouter(prefix="/api/v1/specialists", tags=["specialist"])


@router.get("", response_model=SpecialistListResponse)
async def list_specialists(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SpecialistListResponse:
    """Return the firm's specialists, sorted by display_name. Empty
    list (not 404) when the firm has not been seeded yet."""
    async with firm_context(user.firm_id):
        rows = (
            await session.execute(
                select(Specialist).order_by(Specialist.display_name.asc())
            )
        ).scalars().all()
    return SpecialistListResponse(
        specialists=[SpecialistSummary.model_validate(r) for r in rows]
    )


@router.get(
    "/{specialist_id}/prompt",
    response_model=SpecialistPromptResponse,
)
async def get_specialist_prompt(
    specialist_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SpecialistPromptResponse:
    """Return the active prompt for one specialist. RLS makes
    cross-firm rows invisible -> 404. A specialist row with no
    active version (only possible mid-seed) also returns 404 so
    the frontend never has to render a blank textarea."""
    async with firm_context(user.firm_id):
        spec = (
            await session.execute(
                select(Specialist).where(Specialist.id == specialist_id)
            )
        ).scalar_one_or_none()
        if spec is None or spec.active_version_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="specialist not found",
            )
        version = (
            await session.execute(
                select(SpecialistPromptVersion).where(
                    SpecialistPromptVersion.id == spec.active_version_id
                )
            )
        ).scalar_one()
    return SpecialistPromptResponse(
        id=spec.id,
        name=spec.name,
        display_name=spec.display_name,
        prompt_text=version.prompt_text,
        version_number=version.version_number,
        updated_at=spec.updated_at,
    )


@router.put(
    "/{specialist_id}/prompt",
    response_model=SpecialistPromptResponse,
    dependencies=[Depends(require_role("owner", "principal"))],
)
async def update_specialist_prompt(
    specialist_id: uuid.UUID,
    body: SpecialistPromptUpdate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SpecialistPromptResponse:
    """Insert a new active version, retire the previous active,
    write an audit log row. All in one transaction."""
    async with firm_context(user.firm_id):
        spec = (
            await session.execute(
                select(Specialist).where(Specialist.id == specialist_id)
            )
        ).scalar_one_or_none()
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="specialist not found",
            )

        prev_version_number = 0
        if spec.active_version_id is not None:
            prev_active = (
                await session.execute(
                    select(SpecialistPromptVersion).where(
                        SpecialistPromptVersion.id == spec.active_version_id
                    )
                )
            ).scalar_one()
            prev_version_number = prev_active.version_number
            # Retire first so the partial unique index has room for
            # the new active row inserted next.
            await session.execute(
                update(SpecialistPromptVersion)
                .where(SpecialistPromptVersion.id == spec.active_version_id)
                .values(status="retired")
            )

        new_version = SpecialistPromptVersion(
            firm_id=user.firm_id,
            specialist_id=spec.id,
            version_number=prev_version_number + 1,
            prompt_text=body.prompt_text,
            status="active",
            change_summary=body.change_summary,
            created_by_user_id=user.id,
        )
        session.add(new_version)
        await session.flush()

        spec.active_version_id = new_version.id
        await session.flush()

        await append_audit(
            session,
            firm_id=str(user.firm_id),
            actor_type="user",
            actor_id=str(user.id),
            action="specialist.prompt_updated",
            target_type="specialist",
            target_id=str(spec.id),
            payload={
                "change_summary": body.change_summary,
                "prev_version": prev_version_number,
                "new_version": new_version.version_number,
            },
        )
        await session.commit()

        # Re-read the row after commit so updated_at reflects the
        # onupdate trigger fired by the active_version_id assignment.
        await session.refresh(spec)

    return SpecialistPromptResponse(
        id=spec.id,
        name=spec.name,
        display_name=spec.display_name,
        prompt_text=new_version.prompt_text,
        version_number=new_version.version_number,
        updated_at=spec.updated_at,
    )
