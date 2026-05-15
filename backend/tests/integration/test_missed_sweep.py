"""Integration tests for ``sweep_missed_backfill``.

The sweep walks every active firm's subscriptions where
``last_missed_at IS NOT NULL`` and reconciles each. Graph mail
listing + Microsoft token refresh are mocked via respx; Redis is
the test instance.
"""
import datetime as _dt
import json
import re
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
from coworker.graph.missed_sweep import sweep_missed_backfill
from coworker.security.encryption import encrypt_str
from coworker.workers.plugin_queue import PluginEventQueue

_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
_LOGIN_URL_RE = re.compile(
    r"^https://login\.microsoftonline\.com/[^/]+/oauth2/v2\.0/token$"
)
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
async def sweep_env(test_database_url) -> AsyncIterator[dict]:
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
        yield {"sm": sm, "queue": queue, "redis": redis, "created": created}
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


async def _seed_firm_user_sub(
    sm,
    *,
    last_missed_at: _dt.datetime | None,
    token_expires_at: _dt.datetime | None,
) -> tuple[uuid.UUID, str, str]:
    """Returns (firm_id, firm_slug, azure_oid)."""
    firm_id = uuid.uuid4()
    firm_id_str = str(firm_id)
    azure_oid = f"oid-{uuid.uuid4().hex[:12]}"
    slug = f"ms-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id, name="Sweep Firm", slug=slug,
            azure_tenant_id=str(uuid.uuid4()),
            azure_client_id=str(uuid.uuid4()),
            azure_client_secret_ciphertext=encrypt_str(
                "client-secret", firm_id=firm_id_str,
            ),
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=azure_oid,
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Test User",
            ms_access_token_ciphertext=encrypt_str(
                "stored-access", firm_id=firm_id_str,
            ),
            ms_refresh_token_ciphertext=encrypt_str(
                "stored-refresh", firm_id=firm_id_str,
            ),
            ms_token_expires_at=token_expires_at,
        )
        session.add_all([firm, user])
        await session.flush()
        session.add(
            GraphSubscription(
                firm_id=firm_id,
                user_id=user.id,
                subscription_id=f"sub-{uuid.uuid4().hex[:8]}",
                resource=(
                    f"users/{azure_oid}/mailFolders('Inbox')/messages"
                ),
                notification_url="https://example.com/webhooks/graph/test",
                change_type="created,updated",
                client_state_ciphertext=encrypt_str(
                    "secret", firm_id=firm_id_str,
                ),
                expiration_date_time=_dt.datetime.now(_dt.UTC)
                + _dt.timedelta(days=2),
                last_renewed_at=_dt.datetime.now(_dt.UTC)
                - _dt.timedelta(hours=1),
                last_missed_at=last_missed_at,
            )
        )
        await session.commit()
    return firm_id, slug, azure_oid


def _msg(msg_id: str, received_at: _dt.datetime) -> dict:
    return {
        "id": msg_id,
        "subject": f"Subject {msg_id}",
        "from": {
            "emailAddress": {
                "name": "Sender",
                "address": "sender@example.com",
            }
        },
        "receivedDateTime": received_at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "bodyPreview": "preview",
        "isRead": False,
        "hasAttachments": False,
    }


async def _queue_size(redis) -> int:
    return await redis.llen("queue:plugin_events")


# ===========================================================================
# Tests
# ===========================================================================


async def test_sweep_visits_marked_rows_only(sweep_env) -> None:
    """Rows without last_missed_at are not visited."""
    sm = sweep_env["sm"]
    redis = sweep_env["redis"]

    # ``now`` is the sweep's *cutoff* clock; token-refresh uses the
    # real wall-clock, so anchor ``far_future`` there so the
    # stored token doesn't look stale once today's date moves
    # past the hardcoded one.
    now = _dt.datetime(2026, 5, 14, 13, 0, tzinfo=_dt.UTC)
    far_future = _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=2)
    # Marked firm.
    marked_id, _, _ = await _seed_firm_user_sub(
        sm, last_missed_at=now - _dt.timedelta(minutes=1),
        token_expires_at=far_future,
    )
    sweep_env["created"].append(marked_id)
    # Unmarked firm.
    unmarked_id, _, _ = await _seed_firm_user_sub(
        sm, last_missed_at=None,
        token_expires_at=far_future,
    )
    sweep_env["created"].append(unmarked_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={"value": [_msg("m1", now - _dt.timedelta(minutes=5))]},
            ),
        )
        result = await sweep_missed_backfill(
            sessionmaker=sm,
            queue=sweep_env["queue"],
            now=now,
            firm_ids=[marked_id, unmarked_id],
        )

    assert result.firms_seen == 2
    assert result.rows_visited == 1
    assert result.messages_enqueued == 1
    assert result.actions == {"enqueued": 1}
    assert await _queue_size(redis) == 1


async def test_sweep_clears_marker_when_no_messages(sweep_env) -> None:
    sm = sweep_env["sm"]

    # ``now`` is the sweep's *cutoff* clock; token-refresh uses the
    # real wall-clock, so anchor ``far_future`` there so the
    # stored token doesn't look stale once today's date moves
    # past the hardcoded one.
    now = _dt.datetime(2026, 5, 14, 13, 0, tzinfo=_dt.UTC)
    far_future = _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=2)
    firm_id, _, _ = await _seed_firm_user_sub(
        sm, last_missed_at=now - _dt.timedelta(minutes=1),
        token_expires_at=far_future,
    )
    sweep_env["created"].append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": []}),
        )
        result = await sweep_missed_backfill(
            sessionmaker=sm,
            queue=sweep_env["queue"],
            now=now,
            firm_ids=[firm_id],
        )

    assert result.actions == {"cleared_no_messages": 1}

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.firm_id == firm_id)
            )
        ).scalar_one()
        assert row.last_missed_at is None


async def test_sweep_refreshes_near_expiry_token(sweep_env) -> None:
    """A near-expiry user token gets refreshed before the Graph list call.

    Token-refresh decision uses wall-clock now (not the injectable
    ``now`` arg — that's only the sweep's cutoff), so we anchor
    ``token_expires_at`` to real time.
    """
    sm = sweep_env["sm"]

    real_now = _dt.datetime.now(_dt.UTC)
    near = real_now + _dt.timedelta(minutes=2)
    firm_id, _, _ = await _seed_firm_user_sub(
        sm, last_missed_at=real_now - _dt.timedelta(minutes=1),
        token_expires_at=near,
    )
    sweep_env["created"].append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "Mail.Read",
                },
            ),
        )
        rmock.get(_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": []}),
        )
        result = await sweep_missed_backfill(
            sessionmaker=sm,
            queue=sweep_env["queue"],
            firm_ids=[firm_id],
        )

    # Refresh happened (no token-refresh failure).
    assert "skipped_no_ctx" not in result.actions


async def test_sweep_skips_user_with_no_token(sweep_env) -> None:
    """A subscription whose user has no access token at all is skipped."""
    sm = sweep_env["sm"]

    now = _dt.datetime(2026, 5, 14, 13, 0, tzinfo=_dt.UTC)
    firm_id, _, _ = await _seed_firm_user_sub(
        sm, last_missed_at=now - _dt.timedelta(minutes=1),
        token_expires_at=None,
    )
    sweep_env["created"].append(firm_id)

    # Clear the user's access token after seeding.
    async with sm() as session, firm_context(firm_id):
        await session.execute(
            text(
                "UPDATE users SET ms_access_token_ciphertext = NULL "
                "WHERE firm_id = :id"
            ),
            {"id": str(firm_id)},
        )
        await session.commit()

    with respx.mock(assert_all_called=False):
        result = await sweep_missed_backfill(
            sessionmaker=sm,
            queue=sweep_env["queue"],
            now=now,
            firm_ids=[firm_id],
        )

    assert result.actions == {"skipped_no_ctx": 1}


async def test_sweep_isolates_per_firm_errors(sweep_env) -> None:
    """A Graph 5xx for one firm doesn't abort the sibling firm's row."""
    sm = sweep_env["sm"]
    redis = sweep_env["redis"]

    # ``now`` is the sweep's *cutoff* clock; token-refresh uses the
    # real wall-clock, so anchor ``far_future`` there so the
    # stored token doesn't look stale once today's date moves
    # past the hardcoded one.
    now = _dt.datetime(2026, 5, 14, 13, 0, tzinfo=_dt.UTC)
    far_future = _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=2)
    bad_id, _, bad_oid = await _seed_firm_user_sub(
        sm, last_missed_at=now - _dt.timedelta(minutes=1),
        token_expires_at=far_future,
    )
    sweep_env["created"].append(bad_id)
    good_id, _, good_oid = await _seed_firm_user_sub(
        sm, last_missed_at=now - _dt.timedelta(minutes=1),
        token_expires_at=far_future,
    )
    sweep_env["created"].append(good_id)

    # Dispatch the GET responses by Authorization-irrelevant URL; we
    # discriminate using the resource being queried via $filter
    # isn't possible — both rows hit the same /me/messages URL but
    # with different bearer tokens. Use side_effect to return errors
    # based on call count instead.
    call_count = {"n": 0}

    def _dispatch(request):
        call_count["n"] += 1
        # First call (bad firm) -> 503. Second call (good firm) -> 200.
        if call_count["n"] == 1:
            return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(
            200,
            json={"value": [_msg("m-good", now - _dt.timedelta(minutes=2))]},
        )

    with respx.mock(assert_all_called=False) as rmock:
        rmock.get(_MESSAGES_URL).mock(side_effect=_dispatch)
        result = await sweep_missed_backfill(
            sessionmaker=sm,
            queue=sweep_env["queue"],
            now=now,
            firm_ids=[bad_id, good_id],
        )

    assert result.firms_seen == 2
    assert result.rows_visited == 2
    # One success, one connector error.
    assert result.actions.get("enqueued", 0) == 1
    assert result.actions.get("connector_error", 0) == 1

    # The good firm's message landed in the queue.
    raw = await redis.lrange("queue:plugin_events", 0, -1)
    events = [json.loads(r) for r in raw]
    assert {e["event_data"]["message_id"] for e in events} == {"m-good"}
    assert good_oid and bad_oid  # silence linter
