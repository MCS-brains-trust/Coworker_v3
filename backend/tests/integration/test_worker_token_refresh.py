"""Tests for the worker's per-user token refresh path.

``_resolve_graph_ctx_for_email`` proactively refreshes
``user.ms_access_token`` when it's near expiry; without this,
every email tool 401s an hour after the user's OAuth handshake.

Microsoft's token endpoint is mocked via respx. We verify:
- Fresh token: no refresh, stored token used
- Near-expiry: refresh runs, new token persisted + used
- Expiry missing: treated as near-expiry
- 4xx from Microsoft: graph_ctx is None
- 5xx from Microsoft: fall back to stored token
"""
import datetime as _dt
import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import decrypt_str, encrypt_str
from coworker.workers.plugin_queue import PluginEvent
from coworker.workers.processor import _resolve_graph_ctx_for_email

_LOGIN_URL_RE = re.compile(
    r"^https://login\.microsoftonline\.com/[^/]+/oauth2/v2\.0/token$"
)


@pytest_asyncio.fixture
async def refresh_env(test_database_url) -> AsyncIterator[dict]:
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
    tables = ("firms", "users", "audit_log")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in ("audit_log", "users"):
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
    expires_at: _dt.datetime | None,
    azure_oid: str = "oid-1",
) -> tuple[uuid.UUID, uuid.UUID]:
    firm_id = uuid.uuid4()
    firm_id_str = str(firm_id)
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id,
            name="Refresh Firm",
            slug=f"r-{uuid.uuid4().hex[:8]}",
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
            ms_token_expires_at=expires_at,
        )
        session.add_all([firm, user])
        await session.commit()
        return firm_id, user.id


def _event_for(azure_oid: str = "oid-1") -> PluginEvent:
    return PluginEvent(
        event_id=uuid.uuid4(),
        trigger="email_received",
        firm_slug="test-firm",
        firm_id=uuid.uuid4(),  # not used by the resolver
        event_data={
            "message_id": "msg-1",
            "change_type": "created",
            "resource": f"users/{azure_oid}/messages/msg-1",
        },
        enqueued_at=_dt.datetime.now(_dt.UTC),
    )


def _token_endpoint_response(
    access: str = "fresh-access",
    refresh: str | None = None,
    expires_in: int = 3600,
) -> dict:
    body: dict = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": " ".join([
            "User.Read", "Mail.Read", "Mail.Send",
        ]),  # not exhaustive, doesn't matter for the test
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


# ===========================================================================
# Tests
# ===========================================================================


async def test_fresh_token_skips_refresh(refresh_env) -> None:
    """A token comfortably in the future is used as-is, no HTTP call."""
    sm = refresh_env["sm"]
    far_future = _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1)
    firm_id, _ = await _seed(sm, expires_at=far_future)
    refresh_env["created"].append(firm_id)

    with respx.mock(assert_all_called=False) as rmock:
        route = rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(
                200, json=_token_endpoint_response("should-not-be-used"),
            ),
        )
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            ctx = await _resolve_graph_ctx_for_email(
                session, firm=firm, event=_event_for(),
            )

    assert ctx is not None
    assert ctx.access_token == "stored-access"
    assert not route.called


async def test_near_expiry_refreshes_and_uses_new_token(refresh_env) -> None:
    """Token within the 5-minute refresh buffer triggers a refresh."""
    sm = refresh_env["sm"]
    near = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=2)
    firm_id, user_id = await _seed(sm, expires_at=near)
    refresh_env["created"].append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(
                200,
                json=_token_endpoint_response(
                    access="rotated-access", refresh="rotated-refresh",
                ),
            ),
        )
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            ctx = await _resolve_graph_ctx_for_email(
                session, firm=firm, event=_event_for(),
            )

    assert ctx is not None
    assert ctx.access_token == "rotated-access"

    # New tokens were persisted.
    async with sm() as session, firm_context(firm_id):
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        assert decrypt_str(
            user.ms_access_token_ciphertext, firm_id=str(firm_id),
        ) == "rotated-access"
        assert decrypt_str(
            user.ms_refresh_token_ciphertext, firm_id=str(firm_id),
        ) == "rotated-refresh"


async def test_missing_expires_at_treated_as_near_expiry(refresh_env) -> None:
    """A null ms_token_expires_at forces a refresh on the next call."""
    sm = refresh_env["sm"]
    firm_id, _ = await _seed(sm, expires_at=None)
    refresh_env["created"].append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(
                200, json=_token_endpoint_response("from-null-expiry"),
            ),
        )
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            ctx = await _resolve_graph_ctx_for_email(
                session, firm=firm, event=_event_for(),
            )

    assert ctx is not None
    assert ctx.access_token == "from-null-expiry"


async def test_refresh_4xx_returns_none(refresh_env) -> None:
    """Microsoft rejecting the refresh -> graph_ctx is None."""
    sm = refresh_env["sm"]
    near = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=1)
    firm_id, _ = await _seed(sm, expires_at=near)
    refresh_env["created"].append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(
                400, json={"error": "invalid_grant"},
            ),
        )
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            ctx = await _resolve_graph_ctx_for_email(
                session, firm=firm, event=_event_for(),
            )

    assert ctx is None


async def test_refresh_5xx_falls_back_to_stored_token(refresh_env) -> None:
    """Microsoft 5xx -> use the stored token; downstream call may 401."""
    sm = refresh_env["sm"]
    near = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=1)
    firm_id, _ = await _seed(sm, expires_at=near)
    refresh_env["created"].append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(url__regex=_LOGIN_URL_RE).mock(
            return_value=httpx.Response(503, json={"error": "transient"}),
        )
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            ctx = await _resolve_graph_ctx_for_email(
                session, firm=firm, event=_event_for(),
            )

    assert ctx is not None
    assert ctx.access_token == "stored-access"
