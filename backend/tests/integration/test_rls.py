"""Integration tests for cross-firm Row-Level Security isolation.

These tests prove that the FORCE RLS policies created by Phase 2.1
plus the application-side firm_context contextvar / SQLAlchemy listener
together enforce tenant isolation at the database layer, including
across pool reuse.

Test data setup
---------------
Seed data (Firm rows for two firms + one audit entry each) cannot be
inserted as the application role under FORCE RLS without first knowing
the firm_id and entering firm_context — a chicken-and-egg problem for
firms.id, which IS the firm_id.

We resolve it by temporarily relaxing the FORCE attribute within the
seeding transaction: the application role owns these tables and is
allowed to ALTER them, so we drop FORCE, INSERT the seeded rows, and
restore FORCE before any of the test's verification queries run. The
NO-FORCE window is bounded by the same transaction the seed ran in;
once we restore FORCE, all subsequent reads in the test go through
RLS policies as a non-owner-effective role.

The first three tests use the shared db_session fixture (with its
outer-transaction-rollback safety net for cleanup). The pool-reuse
test builds its own pool_size=1 engine and cleans up explicitly,
because the whole point of that test is to verify behaviour across
genuine pool checkin/checkout — which the savepoint-wrapped fixture
cannot exercise.
"""
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coworker.config import get_settings
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context


_TENANT_TABLES = ("firms", "users", "audit_log")


async def _seed_two_firms(
    session: AsyncSession, firm_a_id: uuid.UUID, firm_b_id: uuid.UUID
) -> None:
    """Insert Firm A, Firm B, and one audit entry per firm.

    Brackets the inserts with ALTER TABLE ... NO FORCE / FORCE so the
    application role (table owner) can write seed data despite FORCE
    RLS being in effect for the rest of the transaction.
    """
    for table in _TENANT_TABLES:
        await session.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))

    common = dict(
        timezone="UTC",
        shadow_mode=True,
        is_active=True,
        sharepoint_clients_folder_path="/",
        settings={},
    )
    session.add(Firm(id=firm_a_id, name="Firm A", slug=f"firm-a-{firm_a_id.hex[:8]}", **common))
    session.add(Firm(id=firm_b_id, name="Firm B", slug=f"firm-b-{firm_b_id.hex[:8]}", **common))
    await session.flush()

    session.add(
        AuditLogEntry(
            firm_id=firm_a_id,
            actor_type="system",
            action="seed",
            payload={"firm": "A"},
            prev_hash="0" * 64,
            entry_hash=f"a{uuid.uuid4().hex}{'0' * 31}"[:64],
        )
    )
    session.add(
        AuditLogEntry(
            firm_id=firm_b_id,
            actor_type="system",
            action="seed",
            payload={"firm": "B"},
            prev_hash="0" * 64,
            entry_hash=f"b{uuid.uuid4().hex}{'0' * 31}"[:64],
        )
    )
    await session.flush()

    for table in _TENANT_TABLES:
        await session.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


async def _set_firm_id(session: AsyncSession, firm_id: uuid.UUID | None) -> None:
    """Apply or clear the app.firm_id GUC for the current transaction.

    The Session "after_begin" listener fires only at transaction start;
    tests that switch firm context mid-transaction (because the
    db_session fixture wraps everything in a single outer transaction)
    issue this directly. Production request handlers would never need
    to switch mid-transaction — each request is its own transaction
    and the listener applies the GUC once.
    """
    value = "" if firm_id is None else str(firm_id)
    await session.execute(
        text("SELECT set_config('app.firm_id', :v, true)"),
        {"v": value},
    )


@pytest.mark.asyncio
async def test_rls_isolates_select_by_firm(db_session: AsyncSession) -> None:
    """Under firm_context(A), only A's data is visible; switch to B and only B's is."""
    firm_a_id = uuid.uuid4()
    firm_b_id = uuid.uuid4()
    await _seed_two_firms(db_session, firm_a_id, firm_b_id)

    async with firm_context(firm_a_id):
        await _set_firm_id(db_session, firm_a_id)
        rows = (await db_session.execute(select(AuditLogEntry))).scalars().all()
        assert {r.firm_id for r in rows} == {firm_a_id}, (
            f"firm_context A should see only A's audit entries, got firm_ids={sorted(str(r.firm_id) for r in rows)}"
        )
        firms_visible = (await db_session.execute(select(Firm.id))).scalars().all()
        assert set(firms_visible) == {firm_a_id}, (
            f"firm_context A should see only firm A in firms table, got {sorted(map(str, firms_visible))}"
        )

    async with firm_context(firm_b_id):
        await _set_firm_id(db_session, firm_b_id)
        rows = (await db_session.execute(select(AuditLogEntry))).scalars().all()
        assert {r.firm_id for r in rows} == {firm_b_id}, (
            f"firm_context B should see only B's audit entries, got firm_ids={sorted(str(r.firm_id) for r in rows)}"
        )


@pytest.mark.asyncio
async def test_rls_no_firm_context_returns_zero_rows(db_session: AsyncSession) -> None:
    """No firm context set → RLS predicate is NULL → zero rows visible."""
    firm_a_id = uuid.uuid4()
    firm_b_id = uuid.uuid4()
    await _seed_two_firms(db_session, firm_a_id, firm_b_id)

    # Make sure no GUC is set (the seeding helper restored FORCE but did
    # not set a firm_id; we explicitly clear here in case something
    # earlier in the test session had set one).
    await _set_firm_id(db_session, None)

    audit_rows = (await db_session.execute(select(AuditLogEntry))).scalars().all()
    assert audit_rows == [], (
        f"No firm_context should return zero audit rows (secure-by-default); "
        f"got {len(audit_rows)} rows for firms {sorted(str(r.firm_id) for r in audit_rows)}"
    )

    firm_rows = (await db_session.execute(select(Firm))).scalars().all()
    assert firm_rows == [], (
        f"No firm_context should return zero firm rows (secure-by-default); "
        f"got {len(firm_rows)} rows"
    )


@pytest.mark.asyncio
async def test_rls_insert_respects_firm_context(db_session: AsyncSession) -> None:
    """Under firm_context(A), inserting a User with firm_id=A succeeds and the row is associated with A."""
    firm_a_id = uuid.uuid4()
    firm_b_id = uuid.uuid4()
    await _seed_two_firms(db_session, firm_a_id, firm_b_id)

    async with firm_context(firm_a_id):
        await _set_firm_id(db_session, firm_a_id)

        user = User(
            firm_id=firm_a_id,
            azure_object_id=f"oid-{uuid.uuid4().hex}",
            upn=f"alice-{uuid.uuid4().hex[:8]}@a.local",
            display_name="Alice",
            role="accountant",
            is_active_processor=False,
            is_reception_mode=False,
        )
        db_session.add(user)
        await db_session.flush()

        found = (
            await db_session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert found.firm_id == firm_a_id, (
            f"User created under firm_context(A) should be bound to firm A, got {found.firm_id}"
        )


@pytest.mark.asyncio
async def test_rls_pool_reuse_does_not_leak_firm_context() -> None:
    """The critical test: pool checkin must clear app.firm_id between sessions.

    Builds a dedicated engine with pool_size=1, max_overflow=0 so the two
    sequential sessions are forced onto the same underlying connection.
    Session 1 deliberately uses set_config(..., is_local=false) — i.e.
    SET (no LOCAL) — to leave a session-level GUC behind that survives
    COMMIT, simulating a future buggy code path that forgets to use
    is_local. Without the pool checkin handler attached in Step 2, the
    connection returns to the pool with that GUC still set, and session 2
    (which enters NO firm_context) inherits it and can read firm A's
    data. With the checkin handler running RESET app.firm_id on
    connection return, session 2 starts clean and the secure-by-default
    behaviour holds.

    This test commits its seed data outside the savepoint-rollback
    fixture, so it cleans up explicitly in a finally block.
    """
    settings = get_settings()
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=False,
        echo=False,
    )
    _attach_pool_listeners(engine)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    firm_a_id = uuid.uuid4()
    firm_b_id = uuid.uuid4()

    async def _cleanup() -> None:
        async with sm() as cleanup:
            for table in _TENANT_TABLES:
                await cleanup.execute(
                    text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
                )
            await cleanup.execute(
                text("DELETE FROM audit_log WHERE firm_id = ANY(:ids)"),
                {"ids": [firm_a_id, firm_b_id]},
            )
            await cleanup.execute(
                text("DELETE FROM users WHERE firm_id = ANY(:ids)"),
                {"ids": [firm_a_id, firm_b_id]},
            )
            await cleanup.execute(
                text("DELETE FROM firms WHERE id = ANY(:ids)"),
                {"ids": [firm_a_id, firm_b_id]},
            )
            for table in _TENANT_TABLES:
                await cleanup.execute(
                    text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
                )
            await cleanup.commit()

    try:
        # Seed both firms — commits to DB so subsequent sessions see them.
        async with sm() as setup:
            await _seed_two_firms(setup, firm_a_id, firm_b_id)
            await setup.commit()

        # Session 1: deliberately leak a session-level GUC.
        # is_local=false means the SET survives COMMIT and persists on
        # the connection until the connection is closed or RESET.
        async with sm() as s1:
            async with s1.begin():
                await s1.execute(
                    text("SELECT set_config('app.firm_id', :v, false)"),
                    {"v": str(firm_a_id)},
                )
                rows = (await s1.execute(select(AuditLogEntry))).scalars().all()
                assert {r.firm_id for r in rows} == {firm_a_id}, (
                    f"session 1 with GUC=A should see only A, got {[str(r.firm_id) for r in rows]}"
                )
            # commit() at end of begin() block ends the transaction, but
            # because we used is_local=false the GUC persists on the
            # connection itself. Closing the session returns the
            # connection to the pool — the checkin handler must RESET.

        # Session 2: NO firm_context, NO _set_firm_id call. If the
        # checkin handler from Step 2 fired correctly, the GUC is
        # cleared and this query returns zero rows. If the GUC leaked,
        # this query returns firm A's data.
        async with sm() as s2:
            async with s2.begin():
                rows = (await s2.execute(select(AuditLogEntry))).scalars().all()
                assert rows == [], (
                    f"pool leaked firm context across checkin: session 2 saw "
                    f"{len(rows)} rows ({sorted(str(r.firm_id) for r in rows)}) when no "
                    f"firm_context was set. Expected zero rows (secure-by-default). "
                    f"This means the connection-pool checkin handler did not run "
                    f"RESET app.firm_id, or ran it incorrectly."
                )
    finally:
        await _cleanup()
        await engine.dispose()
