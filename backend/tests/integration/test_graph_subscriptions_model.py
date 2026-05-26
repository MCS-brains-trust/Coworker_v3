"""Integration tests for the ``graph_subscriptions`` schema.

Verifies the migration's column types, RLS, and uniqueness
constraints behave as designed. The bootstrap function that uses
this table arrives in Phase 11-2; this commit just lands the
storage layer.
"""
import datetime as _dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import Firm, GraphSubscription, User
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest_asyncio.fixture
async def gs_env(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = ("firms", "users", "audit_log", "graph_subscriptions")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            await session.execute(
                text("DELETE FROM graph_subscriptions WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM users WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM audit_log WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"),
                {"id": str(firm_id)},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


async def _seed_firm_and_user(sm) -> tuple[uuid.UUID, uuid.UUID]:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id, name="GS Firm", slug=f"gs-{uuid.uuid4().hex[:8]}",
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Test User",
        )
        session.add_all([firm, user])
        await session.commit()
        return firm_id, user.id


def _sub(
    *,
    firm_id: uuid.UUID,
    user_id: uuid.UUID,
    subscription_id: str = "sub-1",
    resource: str = "users/oid-1/mailFolders('Inbox')/messages",
) -> GraphSubscription:
    return GraphSubscription(
        firm_id=firm_id,
        user_id=user_id,
        subscription_id=subscription_id,
        resource=resource,
        notification_url="https://example.com/api/v1/webhooks/graph/test-firm",
        change_type="created,updated",
        client_state_ciphertext=b"\x00\x01\x02",
        expiration_date_time=_dt.datetime.now(_dt.UTC)
        + _dt.timedelta(days=2),
    )


# ===========================================================================
# Tests
# ===========================================================================


async def test_insert_and_select_under_firm_context(gs_env) -> None:
    sm = gs_env["sm"]
    firm_id, user_id = await _seed_firm_and_user(sm)
    gs_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(_sub(firm_id=firm_id, user_id=user_id))
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        rows = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.firm_id == firm_id)
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.subscription_id == "sub-1"
        assert row.change_type == "created,updated"
        assert row.last_renewed_at is None
        assert row.client_state_ciphertext == b"\x00\x01\x02"


async def test_rls_blocks_select_without_firm_context(gs_env) -> None:
    """Without firm_context, the RLS predicate evaluates to NULL and
    SELECT returns zero rows even though data exists for that firm."""
    sm = gs_env["sm"]
    firm_id, user_id = await _seed_firm_and_user(sm)
    gs_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(_sub(firm_id=firm_id, user_id=user_id))
        await session.commit()

    # No firm_context: RLS hides the row.
    async with sm() as session:
        rows = (
            await session.execute(select(GraphSubscription))
        ).scalars().all()
        assert rows == []


async def test_unique_subscription_id_globally(gs_env) -> None:
    """Two firms can't accidentally collide on the same Graph id.

    Graph guarantees subscription_id is globally unique, so the
    constraint catches programming errors (e.g. forgetting to call
    Graph and inventing an id locally).
    """
    sm = gs_env["sm"]
    firm_id_a, user_a = await _seed_firm_and_user(sm)
    gs_env["created"].append(firm_id_a)
    firm_id_b, user_b = await _seed_firm_and_user(sm)
    gs_env["created"].append(firm_id_b)

    # Seed one row.
    async with sm() as session, firm_context(firm_id_a):
        session.add(
            _sub(
                firm_id=firm_id_a, user_id=user_a,
                subscription_id="dup-id",
            )
        )
        await session.commit()

    # Second firm tries to use the same Graph subscription_id.
    async with sm() as session, firm_context(firm_id_b):
        session.add(
            _sub(
                firm_id=firm_id_b, user_id=user_b,
                subscription_id="dup-id",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_unique_firm_user_resource(gs_env) -> None:
    """A firm can't double-subscribe one user to the same resource."""
    sm = gs_env["sm"]
    firm_id, user_id = await _seed_firm_and_user(sm)
    gs_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            _sub(
                firm_id=firm_id, user_id=user_id,
                subscription_id="sub-a",
                resource="users/oid-1/messages",
            )
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        session.add(
            _sub(
                firm_id=firm_id, user_id=user_id,
                subscription_id="sub-b",
                resource="users/oid-1/messages",  # same resource
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_distinct_resources_allowed_per_user(gs_env) -> None:
    """A user can have multiple subs as long as resources differ."""
    sm = gs_env["sm"]
    firm_id, user_id = await _seed_firm_and_user(sm)
    gs_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            _sub(
                firm_id=firm_id, user_id=user_id,
                subscription_id="sub-msg",
                resource="users/oid-1/messages",
            )
        )
        session.add(
            _sub(
                firm_id=firm_id, user_id=user_id,
                subscription_id="sub-cal",
                resource="users/oid-1/events",
            )
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        rows = (
            await session.execute(select(GraphSubscription))
        ).scalars().all()
        assert len(rows) == 2
        assert {r.resource for r in rows} == {
            "users/oid-1/messages",
            "users/oid-1/events",
        }
