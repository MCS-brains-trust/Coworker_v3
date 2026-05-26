"""Integration tests for ``backfill_missed_for_subscription``.

Graph mail list is mocked via respx; Redis is the test instance.
We verify the catch-up window logic, the enqueue shape (matches
normal webhook events), and the cleanup of ``last_missed_at``.
"""
import asyncio
import datetime as _dt
import json
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import httpx
import pytest_asyncio
import respx
from redis.asyncio import from_url
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.config import get_settings
from coworker.db.models import Firm, GraphSubscription, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.context import GraphContext
from coworker.graph.subscription_backfill import (
    backfill_missed_for_subscription,
)
from coworker.security.encryption import encrypt_str
from coworker.workers.plugin_queue import PluginEventQueue

_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
_TEST_REDIS_DB = "/9"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


def _fresh_test_redis():
    return from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )


@pytest_asyncio.fixture
async def backfill_env(test_database_url) -> AsyncIterator[dict]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    redis = _fresh_test_redis()
    await redis.flushdb()
    queue = PluginEventQueue(redis)

    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "redis": redis, "queue": queue, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await redis.flushdb()
        await redis.aclose()
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
            for t in ("graph_subscriptions", "audit_log", "users"):
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


async def _seed(
    sm,
    *,
    last_renewed_at: _dt.datetime | None,
    last_missed_at: _dt.datetime | None,
) -> tuple[Firm, User, GraphSubscription]:
    firm_id = uuid.uuid4()
    azure_oid = f"oid-{uuid.uuid4().hex[:12]}"
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id,
            name="Backfill Firm",
            slug=f"bf-{uuid.uuid4().hex[:8]}",
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=azure_oid,
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Backfill User",
        )
        session.add_all([firm, user])
        await session.flush()

        sub = GraphSubscription(
            firm_id=firm_id,
            user_id=user.id,
            subscription_id=f"sub-{uuid.uuid4().hex[:8]}",
            resource=(
                f"users/{azure_oid}/mailFolders('Inbox')/messages"
            ),
            notification_url="https://example.com/api/v1/webhooks/graph/test",
            change_type="created,updated",
            client_state_ciphertext=encrypt_str(
                "secret", firm_id=str(firm_id),
            ),
            expiration_date_time=_dt.datetime.now(_dt.UTC)
            + _dt.timedelta(days=2),
            last_renewed_at=last_renewed_at,
            last_missed_at=last_missed_at,
        )
        session.add(sub)
        await session.commit()
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        user = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        sub = (
            await session.execute(
                select(GraphSubscription).where(GraphSubscription.id == sub.id)
            )
        ).scalar_one()
        session.expunge_all()
        return firm, user, sub


def _msg_payload(msg_id: str, received_at: _dt.datetime) -> dict:
    return {
        "id": msg_id,
        "subject": f"Subject {msg_id}",
        "from": {
            "emailAddress": {
                "name": "Sender", "address": "sender@example.com",
            }
        },
        "receivedDateTime": received_at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "bodyPreview": "preview",
        "isRead": False,
        "hasAttachments": False,
    }


async def _queue_contents(redis) -> list[dict]:
    raw = await redis.lrange("queue:plugin_events", 0, -1)
    return [json.loads(r) for r in raw]


# ===========================================================================
# Tests
# ===========================================================================


async def test_backfill_enqueues_messages_since_cutoff(backfill_env) -> None:
    sm = backfill_env["sm"]
    queue = backfill_env["queue"]
    redis = backfill_env["redis"]

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    last_renewed = now - _dt.timedelta(hours=2)
    last_missed = now - _dt.timedelta(minutes=10)

    firm, user, sub = await _seed(
        sm,
        last_renewed_at=last_renewed,
        last_missed_at=last_missed,
    )
    backfill_env["created"].append(firm.id)

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={"value": [
                    _msg_payload("m1", now - _dt.timedelta(minutes=30)),
                    _msg_payload("m2", now - _dt.timedelta(minutes=10)),
                ]},
            )
        )

        async with sm() as session, firm_context(firm.id):
            row = (
                await session.execute(
                    select(GraphSubscription)
                    .where(GraphSubscription.id == sub.id)
                )
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user,
                access_token="user-token", session=session,
            )
            result = await backfill_missed_for_subscription(
                session=session, ctx=ctx, queue=queue,
                row=row, firm_slug=firm.slug, now=now,
            )
            await session.commit()

    assert result.enqueued == 2
    assert route.called

    events = await _queue_contents(redis)
    assert len(events) == 2
    message_ids = {e["event_data"]["message_id"] for e in events}
    assert message_ids == {"m1", "m2"}
    for e in events:
        assert e["trigger"] == "email_received"
        assert e["firm_slug"] == firm.slug
        assert e["firm_id"] == str(firm.id)
        assert e["event_data"]["backfilled"] is True
        assert e["event_data"]["resource"].startswith(
            f"users/{user.azure_object_id}/messages/"
        )

    # last_missed_at cleared; last_renewed_at advanced to now.
    async with sm() as session, firm_context(firm.id):
        refreshed = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.id == sub.id)
            )
        ).scalar_one()
        assert refreshed.last_missed_at is None
        assert refreshed.last_renewed_at is not None
        delta = abs(
            (refreshed.last_renewed_at - now).total_seconds()
        )
        assert delta < 1


async def test_backfill_with_no_marker_is_noop(backfill_env) -> None:
    sm = backfill_env["sm"]
    queue = backfill_env["queue"]

    firm, user, sub = await _seed(
        sm, last_renewed_at=None, last_missed_at=None,
    )
    backfill_env["created"].append(firm.id)

    with respx.mock(assert_all_called=False) as rmock:
        route = rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": []}),
        )

        async with sm() as session, firm_context(firm.id):
            row = (
                await session.execute(
                    select(GraphSubscription)
                    .where(GraphSubscription.id == sub.id)
                )
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user,
                access_token="tok", session=session,
            )
            result = await backfill_missed_for_subscription(
                session=session, ctx=ctx, queue=queue,
                row=row, firm_slug=firm.slug,
            )

    assert result.enqueued == 0
    assert result.skipped == "no_missed_marker"
    assert not route.called


async def test_backfill_empty_window_clears_marker(backfill_env) -> None:
    """The marker is set but Graph returns no messages — clear it anyway."""
    sm = backfill_env["sm"]
    queue = backfill_env["queue"]

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    firm, user, sub = await _seed(
        sm,
        last_renewed_at=now - _dt.timedelta(hours=1),
        last_missed_at=now - _dt.timedelta(minutes=5),
    )
    backfill_env["created"].append(firm.id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": []}),
        )
        async with sm() as session, firm_context(firm.id):
            row = (
                await session.execute(
                    select(GraphSubscription)
                    .where(GraphSubscription.id == sub.id)
                )
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user,
                access_token="tok", session=session,
            )
            result = await backfill_missed_for_subscription(
                session=session, ctx=ctx, queue=queue,
                row=row, firm_slug=firm.slug, now=now,
            )
            await session.commit()

    assert result.skipped == "no_messages"
    assert result.enqueued == 0

    async with sm() as session, firm_context(firm.id):
        refreshed = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.id == sub.id)
            )
        ).scalar_one()
        assert refreshed.last_missed_at is None


async def test_backfill_cutoff_includes_grace_buffer(backfill_env) -> None:
    """The cutoff is last_renewed_at - 5min; verify $filter uses that."""
    sm = backfill_env["sm"]
    queue = backfill_env["queue"]

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    last_renewed = _dt.datetime(2026, 5, 14, 10, 0, tzinfo=_dt.UTC)

    firm, user, sub = await _seed(
        sm,
        last_renewed_at=last_renewed,
        last_missed_at=now - _dt.timedelta(minutes=1),
    )
    backfill_env["created"].append(firm.id)

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": []}),
        )
        async with sm() as session, firm_context(firm.id):
            row = (
                await session.execute(
                    select(GraphSubscription)
                    .where(GraphSubscription.id == sub.id)
                )
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user,
                access_token="tok", session=session,
            )
            await backfill_missed_for_subscription(
                session=session, ctx=ctx, queue=queue,
                row=row, firm_slug=firm.slug, now=now,
            )

    sent = route.calls.last.request
    # Cutoff = 10:00 - 5min = 09:55
    assert "receivedDateTime+gt+2026-05-14T09%3A55%3A00" in str(sent.url)


async def test_backfill_falls_back_to_created_at_when_never_renewed(
    backfill_env,
) -> None:
    """A subscription that was never renewed uses created_at as cutoff."""
    sm = backfill_env["sm"]
    queue = backfill_env["queue"]

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    firm, user, sub = await _seed(
        sm,
        last_renewed_at=None,
        last_missed_at=now - _dt.timedelta(minutes=2),
    )
    backfill_env["created"].append(firm.id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": []}),
        )
        async with sm() as session, firm_context(firm.id):
            row = (
                await session.execute(
                    select(GraphSubscription)
                    .where(GraphSubscription.id == sub.id)
                )
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user,
                access_token="tok", session=session,
            )
            result = await backfill_missed_for_subscription(
                session=session, ctx=ctx, queue=queue,
                row=row, firm_slug=firm.slug, now=now,
            )

    # Just confirm it ran without crashing (the cutoff was derived
    # from created_at). The above test_backfill_cutoff_includes_grace_buffer
    # covers the exact filter format.
    assert result.skipped == "no_messages"


# Suppress unused-import warnings for asyncio (kept for symmetry with
# other test modules that may need it for nested fixtures).
_ = asyncio
