"""Integration tests for ``sweep_subscriptions``.

Real DB; Graph layer mocked via respx. The sweep is the
platform-wide tick the systemd timer fires periodically — we
verify it visits every active firm's active-processor users
and tolerates per-firm and per-user failures.
"""
import datetime as _dt
import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph import subscriptions as subs_module
from coworker.graph.subscription_bootstrap import DEFAULT_SUBSCRIPTION_TTL
from coworker.graph.subscription_sweep import sweep_subscriptions
from coworker.security.encryption import encrypt_str

_LOGIN_URL_RE = re.compile(
    r"^https://login\.microsoftonline\.com/[^/]+/oauth2/v2\.0/token$"
)
_SUBS_URL = "https://graph.microsoft.com/v1.0/subscriptions"
_BASE = "https://example.com"


@pytest_asyncio.fixture(autouse=True)
async def _clear_token_cache():
    subs_module._app_token_cache.clear()
    yield
    subs_module._app_token_cache.clear()


@pytest_asyncio.fixture
async def sweep_env(test_database_url) -> AsyncIterator[dict]:
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
            for t in (
                "graph_subscriptions", "audit_log", "users",
            ):
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


async def _seed_firm(
    sm,
    *,
    slug: str | None = None,
    with_azure_creds: bool = True,
    is_active: bool = True,
) -> uuid.UUID:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        kwargs: dict = {
            "id": firm_id,
            "name": "Sweep Firm",
            "slug": slug or f"sw-{uuid.uuid4().hex[:8]}",
            "is_active": is_active,
        }
        if with_azure_creds:
            kwargs["azure_tenant_id"] = str(uuid.uuid4())
            kwargs["azure_client_id"] = str(uuid.uuid4())
            kwargs["azure_client_secret_ciphertext"] = encrypt_str(
                "secret", firm_id=str(firm_id),
            )
        session.add(Firm(**kwargs))
        await session.commit()
    return firm_id


async def _seed_user(
    sm, firm_id, *, is_active_processor: bool, azure_oid: str | None = None,
) -> uuid.UUID:
    async with sm() as session, firm_context(firm_id):
        user = User(
            firm_id=firm_id,
            azure_object_id=azure_oid or f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Test User",
            is_active_processor=is_active_processor,
        )
        session.add(user)
        await session.commit()
        return user.id


def _token_response(token: str = "tok-1") -> dict:
    return {
        "access_token": token,
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "https://graph.microsoft.com/.default",
    }


def _subscription_response(
    *, sub_id: str, resource: str, expiration: _dt.datetime,
) -> dict:
    return {
        "id": sub_id,
        "resource": resource,
        "changeType": "created,updated",
        "notificationUrl": f"{_BASE}/webhooks/graph/test",
        "expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "clientState": "echo",
        "applicationId": "app-id",
        "creatorId": "creator-id",
    }


# ===========================================================================
# Tests
# ===========================================================================


async def test_sweep_visits_active_users_only(sweep_env) -> None:
    """A passive user (is_active_processor=False) is skipped."""
    sm = sweep_env["sm"]
    firm_id = await _seed_firm(sm, slug="sweep-a")
    sweep_env["created"].append(firm_id)
    active_user_id = await _seed_user(
        sm, firm_id, is_active_processor=True, azure_oid="oid-active",
    )
    await _seed_user(sm, firm_id, is_active_processor=False)

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    expiry = now + DEFAULT_SUBSCRIPTION_TTL

    with respx.mock(assert_all_called=False) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(200, json=_token_response()),
        )
        post = rmock.post(_SUBS_URL).mock(
            return_value=httpx.Response(
                201,
                json=_subscription_response(
                    sub_id="sub-1",
                    resource=(
                        "users/oid-active/mailFolders('Inbox')/messages"
                    ),
                    expiration=expiry,
                ),
            ),
        )

        result = await sweep_subscriptions(
            sessionmaker=sm,
            public_webhook_base_url=_BASE,
            now=now,
            firm_ids=[firm_id],
        )

    assert result.firms_seen == 1
    assert result.users_seen == 1
    assert result.actions == {"created": 1}
    assert post.call_count == 1
    assert active_user_id  # silence linter


async def test_sweep_skips_firm_without_azure_creds(sweep_env) -> None:
    """A firm missing Azure credentials is recorded as a firm_error."""
    sm = sweep_env["sm"]
    firm_id = await _seed_firm(sm, with_azure_creds=False)
    sweep_env["created"].append(firm_id)
    await _seed_user(sm, firm_id, is_active_processor=True)

    with respx.mock(assert_all_called=False):
        result = await sweep_subscriptions(
            sessionmaker=sm,
            public_webhook_base_url=_BASE,
            firm_ids=[firm_id],
        )

    assert result.firms_seen == 1
    assert result.users_seen == 0
    assert len(result.firm_errors) == 1
    assert "ValueError" in result.firm_errors[0]


async def test_sweep_skips_inactive_firm(sweep_env) -> None:
    """list_active_firm_ids excludes is_active=False firms.

    The sweep itself doesn't re-check (since we tell it which
    firm_ids to visit), but the production discovery path goes
    via list_active_firm_ids — exercised here with auto-discovery.
    """
    from coworker.db.firms import list_active_firm_ids

    sm = sweep_env["sm"]
    inactive = await _seed_firm(sm, is_active=False)
    sweep_env["created"].append(inactive)

    async with sm() as session:
        ids = await list_active_firm_ids(session)
        assert inactive not in ids


async def test_sweep_continues_after_per_user_graph_failure(sweep_env) -> None:
    """A 5xx for one user doesn't abort the firm — other users still run."""
    sm = sweep_env["sm"]
    firm_id = await _seed_firm(sm)
    sweep_env["created"].append(firm_id)
    await _seed_user(
        sm, firm_id, is_active_processor=True, azure_oid="oid-bad",
    )
    await _seed_user(
        sm, firm_id, is_active_processor=True, azure_oid="oid-good",
    )

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    expiry = now + DEFAULT_SUBSCRIPTION_TTL

    def _dispatch(request):
        body = request.read().decode()
        if "oid-bad" in body:
            return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(
            201,
            json=_subscription_response(
                sub_id=f"sub-{uuid.uuid4().hex[:6]}",
                resource=(
                    "users/oid-good/mailFolders('Inbox')/messages"
                ),
                expiration=expiry,
            ),
        )

    with respx.mock(assert_all_called=False) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(200, json=_token_response()),
        )
        rmock.post(_SUBS_URL).mock(side_effect=_dispatch)

        result = await sweep_subscriptions(
            sessionmaker=sm,
            public_webhook_base_url=_BASE,
            now=now,
            firm_ids=[firm_id],
        )

    assert result.firms_seen == 1
    assert result.users_seen == 2
    assert result.actions == {"created": 1}
    assert len(result.user_errors) == 1
    assert "ConnectorTransient" in result.user_errors[0]


async def test_sweep_visits_multiple_firms(sweep_env) -> None:
    """Two active firms each get their users subscribed."""
    sm = sweep_env["sm"]
    firm_a = await _seed_firm(sm, slug="firm-a")
    sweep_env["created"].append(firm_a)
    firm_b = await _seed_firm(sm, slug="firm-b")
    sweep_env["created"].append(firm_b)
    await _seed_user(sm, firm_a, is_active_processor=True, azure_oid="oid-a")
    await _seed_user(sm, firm_b, is_active_processor=True, azure_oid="oid-b")

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    expiry = now + DEFAULT_SUBSCRIPTION_TTL

    call_count = {"posts": 0}

    def _dispatch(request):
        call_count["posts"] += 1
        return httpx.Response(
            201,
            json=_subscription_response(
                sub_id=f"sub-{call_count['posts']}",
                resource=f"users/oid-{call_count['posts']}/mailFolders('Inbox')/messages",
                expiration=expiry,
            ),
        )

    with respx.mock(assert_all_called=False) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(200, json=_token_response()),
        )
        rmock.post(_SUBS_URL).mock(side_effect=_dispatch)

        result = await sweep_subscriptions(
            sessionmaker=sm,
            public_webhook_base_url=_BASE,
            now=now,
            firm_ids=[firm_a, firm_b],
        )

    assert result.firms_seen == 2
    assert result.users_seen == 2
    assert result.actions == {"created": 2}
    assert call_count["posts"] == 2


async def test_sweep_empty_base_url_rejected(sweep_env) -> None:
    sm = sweep_env["sm"]
    import pytest
    with pytest.raises(ValueError, match="public_webhook_base_url"):
        await sweep_subscriptions(
            sessionmaker=sm,
            public_webhook_base_url="",
        )
