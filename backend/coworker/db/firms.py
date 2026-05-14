"""Firm-row lookups that must read across firm boundaries.

The slug-keyed lookup pattern shows up wherever we know a firm's slug
but not its id — bootstrap-firm, the OAuth start route, future admin
tooling. Under FORCE ROW LEVEL SECURITY on `firms`, a SELECT cannot
match across firms with `app.firm_id` unset, and we *can't* set it to
a useful value because the whole point of the lookup is to discover
which firm the slug belongs to.

This module wraps the necessary `ALTER TABLE firms NO FORCE / SELECT
/ ALTER TABLE firms FORCE` bracket so callers don't reproduce the
pattern. The `coworker` role owns `firms` so it can ALTER TABLE; FORCE
is restored within the same transaction so on commit the table state
is unchanged. Same shape as `_seed_two_firms` in test_rls.py.

Callers that want to write firm-scoped data after the lookup should
commit (releasing this transaction's NO FORCE/FORCE bracket), then
enter `firm_context(firm.id)` for the write. Doing the write under
the still-open NO FORCE-bracket transaction would also work but
defeats RLS for the duration of the transaction — preferring a
commit-and-re-enter pattern keeps RLS enforcement narrow.
"""
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db.models.tenancy import Firm


async def lookup_firm_by_slug(session: AsyncSession, slug: str) -> Firm | None:
    """Return the Firm with the given slug, or None if no such firm exists.

    Internally lifts FORCE RLS on `firms` for the duration of the
    SELECT. Does NOT enter firm_context; that is the caller's job once
    they know the firm's id.
    """
    await session.execute(text("ALTER TABLE firms NO FORCE ROW LEVEL SECURITY"))
    try:
        return (
            await session.execute(select(Firm).where(Firm.slug == slug))
        ).scalar_one_or_none()
    finally:
        await session.execute(text("ALTER TABLE firms FORCE ROW LEVEL SECURITY"))


async def list_active_firm_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Return active firms' ids for platform-level system sweeps.

    Used by jobs that legitimately cross firm boundaries
    (subscription renewal, token-usage rollups, …). Same NO FORCE
    bracket as ``lookup_firm_by_slug`` — the SELECT crosses RLS
    deliberately, the bracket is restored before the transaction
    commits.

    Returns just the ids (not full ORM objects) so the caller can
    enter a fresh ``firm_context`` per firm without dragging an
    ORM-attached row across that boundary.
    """
    await session.execute(text("ALTER TABLE firms NO FORCE ROW LEVEL SECURITY"))
    try:
        result = await session.execute(
            select(Firm.id).where(Firm.is_active.is_(True))
        )
        return list(result.scalars().all())
    finally:
        await session.execute(text("ALTER TABLE firms FORCE ROW LEVEL SECURITY"))
