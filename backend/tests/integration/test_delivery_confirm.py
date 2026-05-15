"""Integration tests for the 4h delivery-confirmation sweep + the
``mark_delivery_failed`` helper that the
``delivery_status_handler`` plugin invokes (pre-pilot Task 3).

Real DB; no Graph; no Claude. The helpers under test are pure DB
operations gated by RLS.
"""
import datetime as _dt
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.approval.delivery import (
    DELIVERY_CONFIRMATION_WINDOW,
    mark_delivery_failed,
    sweep_delivery_confirmation,
)
from coworker.approval.items import (
    CreateApprovalInput,
    approve,
    create_approval,
)
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest_asyncio.fixture
async def delivery_env(test_database_url) -> AsyncIterator[dict]:
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
    tables = ("firms", "users", "audit_log", "approval_items")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in ("approval_items", "audit_log", "users"):
                await session.execute(
                    text(f"DELETE FROM {t} WHERE firm_id = :id"),
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


async def _seed_firm(sm) -> tuple[uuid.UUID, uuid.UUID]:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id, name="Delivery Firm",
            slug=f"d-{uuid.uuid4().hex[:8]}",
            shadow_mode=False,
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Sender",
        )
        session.add_all([firm, user])
        await session.commit()
        return firm_id, user.id


async def _seed_sent_item(
    sm,
    firm_id,
    *,
    from_user_id,
    internet_message_id: str | None = "<msg-1@graph.local>",
    delivery_status_updated_at: _dt.datetime | None = None,
) -> uuid.UUID:
    """Create an approval_item already in delivery_status='sent'.

    Bypasses the normal pending->approved->sent path; we just want
    a row in the right shape so the sweep and the correlator have
    something to operate on.
    """
    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary="Test draft",
                payload={
                    "from_user_id": str(from_user_id),
                    "to": ["client@example.com"],
                    "subject": "x",
                    "body_html": "<p>x</p>",
                },
            ),
        )
        await session.commit()
        await approve(session, row.id, decided_by_user_id=from_user_id)
        await session.commit()

        # Manually flip into the post-dispatch state. In production
        # this happens via dispatch_email_draft.
        row.status = "sent"
        row.delivery_status = "sent"
        row.executed_internet_message_id = internet_message_id
        row.delivery_status_updated_at = (
            delivery_status_updated_at
            or _dt.datetime.now(_dt.UTC)
        )
        await session.commit()
        return row.id


# ===========================================================================
# mark_delivery_failed
# ===========================================================================


async def test_mark_delivery_failed_correlates_and_flips(
    delivery_env,
) -> None:
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)
    item_id = await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        internet_message_id="<original@graph.local>",
    )

    async with sm() as session, firm_context(firm_id):
        outcome = await mark_delivery_failed(
            session,
            internet_message_id="<original@graph.local>",
            detail="smtp; 550 5.1.1 user unknown",
        )
        await session.commit()

    assert outcome.correlated is True
    assert outcome.approval_item_id == item_id

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.delivery_status == "failed"
        assert row.delivery_status_detail == "smtp; 550 5.1.1 user unknown"
        assert row.delivery_status_updated_at is not None


async def test_mark_delivery_failed_uncorrelated_returns_false(
    delivery_env,
) -> None:
    """An NDR referencing a Message-ID we never persisted -> no row
    found, correlated=False, no exception."""
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)
    await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        internet_message_id="<original@graph.local>",
    )

    async with sm() as session, firm_context(firm_id):
        outcome = await mark_delivery_failed(
            session,
            internet_message_id="<unknown@somewhere.else>",
            detail="some detail",
        )
        await session.commit()

    assert outcome.correlated is False
    assert outcome.approval_item_id is None


async def test_mark_delivery_failed_truncates_detail(
    delivery_env,
) -> None:
    """Detail longer than 500 chars is truncated; everything else
    stays intact."""
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)
    item_id = await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        internet_message_id="<original@graph.local>",
    )

    long_detail = "x" * 5000
    async with sm() as session, firm_context(firm_id):
        await mark_delivery_failed(
            session,
            internet_message_id="<original@graph.local>",
            detail=long_detail,
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.delivery_status_detail is not None
        assert len(row.delivery_status_detail) == 500


# ===========================================================================
# sweep_delivery_confirmation
# ===========================================================================


async def test_sweep_flips_old_sent_to_delivered(delivery_env) -> None:
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)

    now = _dt.datetime.now(_dt.UTC)
    old_updated_at = now - DELIVERY_CONFIRMATION_WINDOW - _dt.timedelta(minutes=5)
    item_id = await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        delivery_status_updated_at=old_updated_at,
    )

    result = await sweep_delivery_confirmation(
        sessionmaker=sm, firm_ids=[firm_id], now=now,
    )
    assert result.firms_seen == 1
    assert result.items_seen == 1
    assert result.confirmed == 1

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.delivery_status == "delivered"
        # status (approval-side) is unchanged — sweep only touches
        # delivery_status.
        assert row.status == "sent"


async def test_sweep_leaves_recent_sent_alone(delivery_env) -> None:
    """Rows inside the 4h window are NOT flipped."""
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)

    now = _dt.datetime.now(_dt.UTC)
    recent_updated_at = now - _dt.timedelta(hours=1)
    item_id = await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        delivery_status_updated_at=recent_updated_at,
    )

    result = await sweep_delivery_confirmation(
        sessionmaker=sm, firm_ids=[firm_id], now=now,
    )
    assert result.confirmed == 0

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.delivery_status == "sent"


async def test_sweep_idempotent(delivery_env) -> None:
    """Once a row is flipped to 'delivered', a second sweep is a
    no-op."""
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)

    now = _dt.datetime.now(_dt.UTC)
    old_updated_at = now - DELIVERY_CONFIRMATION_WINDOW - _dt.timedelta(minutes=5)
    await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        delivery_status_updated_at=old_updated_at,
    )

    first = await sweep_delivery_confirmation(
        sessionmaker=sm, firm_ids=[firm_id], now=now,
    )
    second = await sweep_delivery_confirmation(
        sessionmaker=sm, firm_ids=[firm_id], now=now,
    )
    assert first.confirmed == 1
    assert second.confirmed == 0
    assert second.items_seen == 0


async def test_sweep_counts_skipped_no_internet_id(delivery_env) -> None:
    """Rows missing executed_internet_message_id are still flipped
    to delivered but counted separately so ops can see the
    OWA-regeneration false-positive rate."""
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)

    now = _dt.datetime.now(_dt.UTC)
    old_updated_at = now - DELIVERY_CONFIRMATION_WINDOW - _dt.timedelta(minutes=5)
    await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        internet_message_id=None,
        delivery_status_updated_at=old_updated_at,
    )

    result = await sweep_delivery_confirmation(
        sessionmaker=sm, firm_ids=[firm_id], now=now,
    )
    assert result.confirmed == 1
    assert result.skipped_no_internet_id == 1


async def test_sweep_does_not_touch_failed_rows(delivery_env) -> None:
    """A row already in delivery_status='failed' (an NDR was
    correlated to it) stays failed, even if old."""
    sm = delivery_env["sm"]
    firm_id, user_id = await _seed_firm(sm)
    delivery_env["created"].append(firm_id)

    now = _dt.datetime.now(_dt.UTC)
    old_updated_at = now - DELIVERY_CONFIRMATION_WINDOW - _dt.timedelta(hours=1)
    item_id = await _seed_sent_item(
        sm, firm_id, from_user_id=user_id,
        delivery_status_updated_at=old_updated_at,
    )
    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        row.delivery_status = "failed"
        row.delivery_status_detail = "ndr"
        await session.commit()

    result = await sweep_delivery_confirmation(
        sessionmaker=sm, firm_ids=[firm_id], now=now,
    )
    assert result.confirmed == 0

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.delivery_status == "failed"
