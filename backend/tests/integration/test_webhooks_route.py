"""End-to-end tests for the Graph webhook receiver.

The route is driven through FastAPI's TestClient; Redis is the
real test instance (logical DB 9) so the enqueue side of the
contract is exercised. Microsoft's POST body shapes are faked to
match the documented notification schema.
"""
import asyncio
import json
import uuid
from urllib.parse import urlparse, urlunparse

import pytest_asyncio
from fastapi.testclient import TestClient
from redis.asyncio import from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.api.main import app
from coworker.config import get_settings
from coworker.db.models import Firm, GraphSubscription, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import encrypt_str

_TEST_REDIS_DB = "/9"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


def _fresh_test_redis():
    return from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )


async def _redis_flushdb_oneshot() -> None:
    client = _fresh_test_redis()
    try:
        await client.flushdb()
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def webhook_env(test_database_url, monkeypatch):
    """Wire SessionLocal + Redis + Engine to test instances and seed a firm."""
    from coworker.db import redis as redis_module
    from coworker.db import session as session_module

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)

    redis_module.get_redis.cache_clear()
    monkeypatch.setattr(redis_module, "get_redis", _fresh_test_redis)

    await _redis_flushdb_oneshot()

    firm_id = uuid.uuid4()
    slug = f"webhook-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Webhook Firm", slug=slug))
        await session.commit()

    try:
        yield {"sm": sm, "firm_id": firm_id, "slug": slug}
    finally:
        await _cleanup_firm(sm, firm_id)
        await _redis_flushdb_oneshot()
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
                text("DELETE FROM audit_log WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM users WHERE firm_id = :id"),
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


async def _seed_subscription(
    sm,
    firm_id: uuid.UUID,
    *,
    subscription_id: str = "sub-123",
    client_state: str = "secret",
    resource: str = "users/u-1/mailFolders('Inbox')/messages",
) -> uuid.UUID:
    """Insert a User + GraphSubscription for tests that exercise enqueue.

    Returns the User id so tests can match it against
    notification resource paths if needed.
    """
    import datetime as _dt

    async with sm() as session, firm_context(firm_id):
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Test User",
        )
        session.add(user)
        await session.flush()
        session.add(
            GraphSubscription(
                firm_id=firm_id,
                user_id=user.id,
                subscription_id=subscription_id,
                resource=resource,
                notification_url="https://example.com/webhooks/graph/test",
                change_type="created,updated",
                client_state_ciphertext=encrypt_str(
                    client_state, firm_id=str(firm_id),
                ),
                expiration_date_time=_dt.datetime.now(_dt.UTC)
                + _dt.timedelta(days=2),
            )
        )
        await session.commit()
        return user.id


def _notification(message_id: str = "msg-1", change_type: str = "created") -> dict:
    return {
        "subscriptionId": "sub-123",
        "clientState": "secret",
        "changeType": change_type,
        "resource": "users/u-1/messages/" + message_id,
        "resourceData": {
            "@odata.type": "#Microsoft.Graph.Message",
            "id": message_id,
        },
    }


def _queue_contents() -> list[dict]:
    """Snapshot of the test Redis queue."""

    async def _run() -> list[dict]:
        client = _fresh_test_redis()
        try:
            raw = await client.lrange("queue:plugin_events", 0, -1)
            return [json.loads(r) for r in raw]
        finally:
            await client.aclose()

    return asyncio.run(_run())


# ===========================================================================
# Tests
# ===========================================================================


def test_validation_token_handshake_returns_plain_text(webhook_env) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(
        f"/webhooks/graph/{slug}",
        params={"validationToken": "abc-token-xyz"},
    )
    assert resp.status_code == 200
    assert resp.text == "abc-token-xyz"
    # No enqueue on handshake.
    assert _queue_contents() == []


def test_notification_enqueues_plugin_event(webhook_env) -> None:
    slug = webhook_env["slug"]
    firm_id = webhook_env["firm_id"]
    asyncio.run(_seed_subscription(webhook_env["sm"], firm_id))

    body = {"value": [_notification(message_id="msg-real-1")]}

    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202

    events = _queue_contents()
    assert len(events) == 1
    e = events[0]
    assert e["trigger"] == "email_received"
    assert e["firm_slug"] == slug
    assert e["firm_id"] == str(firm_id)
    assert e["event_data"]["message_id"] == "msg-real-1"
    assert e["event_data"]["change_type"] == "created"
    assert e["event_data"]["subscription_id"] == "sub-123"


def test_multiple_notifications_in_one_post_all_enqueue(webhook_env) -> None:
    slug = webhook_env["slug"]
    asyncio.run(_seed_subscription(webhook_env["sm"], webhook_env["firm_id"]))
    body = {
        "value": [
            _notification(message_id=f"msg-{i}") for i in range(3)
        ]
    }

    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202

    events = _queue_contents()
    assert len(events) == 3
    assert {e["event_data"]["message_id"] for e in events} == {
        "msg-0", "msg-1", "msg-2",
    }


def test_unknown_slug_returns_202_without_enqueuing(webhook_env) -> None:
    client = TestClient(app)
    resp = client.post(
        f"/webhooks/graph/does-not-exist-{uuid.uuid4().hex[:6]}",
        json={"value": [_notification()]},
    )
    # 202 (no leak of slug existence), but nothing enqueued.
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_malformed_json_returns_202(webhook_env) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(
        f"/webhooks/graph/{slug}",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_non_object_body_returns_202(webhook_env) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=["just", "an", "array"])
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_notification_missing_message_id_is_skipped(webhook_env) -> None:
    """A notification lacking resourceData.id is dropped silently."""
    slug = webhook_env["slug"]
    asyncio.run(_seed_subscription(webhook_env["sm"], webhook_env["firm_id"]))
    body = {
        "value": [
            # No resourceData — also lacks clientState, so will be
            # rejected at the validation stage. Either way: no enqueue.
            {"subscriptionId": "sub", "changeType": "created"},
            _notification(message_id="msg-good"),
        ]
    }
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202

    events = _queue_contents()
    assert len(events) == 1
    assert events[0]["event_data"]["message_id"] == "msg-good"


def test_empty_notifications_array_returns_202_without_enqueueing(
    webhook_env,
) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json={"value": []})
    assert resp.status_code == 202
    assert _queue_contents() == []


# ===========================================================================
# Phase 11-4: clientState validation
# ===========================================================================


def test_unknown_subscription_id_is_rejected(webhook_env) -> None:
    """A notification claiming a sub_id we never created is dropped.

    No row exists, validation fails, the receiver returns 202 (so
    Microsoft doesn't retry) and nothing is enqueued.
    """
    slug = webhook_env["slug"]
    asyncio.run(_seed_subscription(
        webhook_env["sm"], webhook_env["firm_id"],
        subscription_id="sub-real", client_state="secret",
    ))

    body = {
        "value": [
            {
                **_notification(),
                "subscriptionId": "sub-fake",
                "clientState": "anything",
            }
        ]
    }
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_wrong_clientstate_is_rejected(webhook_env) -> None:
    """A real subscription id with a fabricated clientState is dropped."""
    slug = webhook_env["slug"]
    asyncio.run(_seed_subscription(
        webhook_env["sm"], webhook_env["firm_id"],
        subscription_id="sub-real", client_state="actual-secret",
    ))

    body = {
        "value": [
            {
                **_notification(),
                "subscriptionId": "sub-real",
                "clientState": "wrong-secret",
            }
        ]
    }
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_cross_firm_subscription_id_is_rejected(webhook_env) -> None:
    """A subscription_id that belongs to firm A is rejected when posted
    to firm B's URL — guards against an attacker who learned a real
    id but tries to direct notifications to a different firm's queue."""
    sm = webhook_env["sm"]
    firm_a_id = webhook_env["firm_id"]  # the fixture's firm

    # Seed firm A with a real subscription.
    asyncio.run(_seed_subscription(
        sm, firm_a_id,
        subscription_id="sub-firm-a", client_state="firm-a-secret",
    ))

    # Seed a second firm B.
    firm_b_id = uuid.uuid4()
    firm_b_slug = f"webhook-b-{uuid.uuid4().hex[:8]}"

    async def _seed_b():
        async with sm() as session, firm_context(firm_b_id):
            session.add(Firm(
                id=firm_b_id, name="Firm B", slug=firm_b_slug,
            ))
            await session.commit()
    asyncio.run(_seed_b())

    try:
        # Notification claims firm-a's sub but is posted to firm-b's URL.
        body = {
            "value": [
                {
                    **_notification(),
                    "subscriptionId": "sub-firm-a",
                    "clientState": "firm-a-secret",
                }
            ]
        }
        client = TestClient(app)
        resp = client.post(f"/webhooks/graph/{firm_b_slug}", json=body)
        assert resp.status_code == 202
        # Validation rejects the cross-firm replay — nothing enqueued.
        assert _queue_contents() == []
        # firm A's queue likewise sees nothing (we posted to B).
    finally:
        asyncio.run(_cleanup_firm(sm, firm_b_id))


def test_valid_subscription_with_correct_clientstate_is_enqueued(
    webhook_env,
) -> None:
    """The happy path: matching sub_id + clientState -> enqueued."""
    slug = webhook_env["slug"]
    firm_id = webhook_env["firm_id"]
    asyncio.run(_seed_subscription(
        webhook_env["sm"], firm_id,
        subscription_id="sub-happy", client_state="correct",
    ))

    body = {
        "value": [
            {
                **_notification(),
                "subscriptionId": "sub-happy",
                "clientState": "correct",
            }
        ]
    }
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202

    events = _queue_contents()
    assert len(events) == 1
    assert events[0]["event_data"]["subscription_id"] == "sub-happy"
