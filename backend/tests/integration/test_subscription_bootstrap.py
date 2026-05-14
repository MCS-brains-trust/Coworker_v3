"""Integration tests for ``ensure_subscription``.

The Graph layer is mocked via respx — we're testing the
create-vs-renew-vs-reuse decision tree, the persistence shape,
the encrypted client_state, and the cleanup of stale rows.
"""
import datetime as _dt
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import Firm, GraphSubscription, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.subscription_bootstrap import (
    DEFAULT_RENEWAL_BUFFER,
    DEFAULT_SUBSCRIPTION_TTL,
    INBOX_MESSAGES_RESOURCE_TEMPLATE,
    ensure_subscription,
)
from coworker.graph.subscriptions import AppGraphContext
from coworker.security.encryption import decrypt_str, encrypt_str

_SUBS_URL = "https://graph.microsoft.com/v1.0/subscriptions"
_NOTIFICATION_URL = "https://example.com/webhooks/graph/test"


@pytest_asyncio.fixture
async def bootstrap_env(test_database_url) -> AsyncIterator[dict]:
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


async def _seed(sm) -> tuple[Firm, User]:
    firm_id = uuid.uuid4()
    azure_oid = f"oid-{uuid.uuid4().hex[:12]}"
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id,
            name="Bootstrap Firm",
            slug=f"b-{uuid.uuid4().hex[:8]}",
            azure_tenant_id=str(uuid.uuid4()),
            azure_client_id=str(uuid.uuid4()),
            azure_client_secret_ciphertext=encrypt_str(
                "secret", firm_id=str(firm_id)
            ),
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=azure_oid,
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Test User",
        )
        session.add_all([firm, user])
        await session.commit()
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        user = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        session.expunge(firm)
        session.expunge(user)
        return firm, user


def _resource(user: User) -> str:
    return INBOX_MESSAGES_RESOURCE_TEMPLATE.format(
        azure_object_id=user.azure_object_id
    )


def _subscription_response(
    *,
    sub_id: str,
    resource: str,
    expiration: _dt.datetime,
) -> dict:
    return {
        "id": sub_id,
        "resource": resource,
        "changeType": "created,updated",
        "notificationUrl": _NOTIFICATION_URL,
        "expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "clientState": "echoed-back",
        "applicationId": "app-id",
        "creatorId": "creator-id",
    }


# ===========================================================================
# Tests
# ===========================================================================


async def test_creates_fresh_subscription_when_no_row(bootstrap_env) -> None:
    sm = bootstrap_env["sm"]
    firm, user = await _seed(sm)
    bootstrap_env["created"].append(firm.id)

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    expiry = now + DEFAULT_SUBSCRIPTION_TTL

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.post(_SUBS_URL).mock(
            return_value=httpx.Response(
                201,
                json=_subscription_response(
                    sub_id="sub-fresh-1",
                    resource=_resource(user),
                    expiration=expiry,
                ),
            )
        )

        async with sm() as session, firm_context(firm.id):
            ctx = AppGraphContext(
                firm=firm, access_token="app-tok", session=session,
            )
            outcome = await ensure_subscription(
                session=session,
                ctx=ctx,
                user=user,
                resource=_resource(user),
                notification_url=_NOTIFICATION_URL,
                now=now,
                client_state_factory=lambda: "fixed-state-1",
            )
            await session.commit()

    assert outcome.action == "created"
    assert outcome.row.subscription_id == "sub-fresh-1"
    assert outcome.row.last_renewed_at is None
    assert route.called

    # The POST body advertises both notification and lifecycle URLs
    # at the same endpoint so the webhook handler can dispatch.
    import json
    sent = json.loads(route.calls.last.request.read().decode())
    assert sent["notificationUrl"] == _NOTIFICATION_URL
    assert sent["lifecycleNotificationUrl"] == _NOTIFICATION_URL

    # Persisted with encrypted client_state.
    async with sm() as session, firm_context(firm.id):
        row = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.firm_id == firm.id)
            )
        ).scalar_one()
        plaintext = decrypt_str(
            row.client_state_ciphertext, firm_id=str(firm.id),
        )
        assert plaintext == "fixed-state-1"


async def test_reuses_existing_row_when_far_from_expiry(bootstrap_env) -> None:
    sm = bootstrap_env["sm"]
    firm, user = await _seed(sm)
    bootstrap_env["created"].append(firm.id)

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    expiry = now + _dt.timedelta(days=2)  # well past renewal_buffer

    async with sm() as session, firm_context(firm.id):
        session.add(
            GraphSubscription(
                firm_id=firm.id,
                user_id=user.id,
                subscription_id="existing-sub",
                resource=_resource(user),
                notification_url=_NOTIFICATION_URL,
                change_type="created,updated",
                client_state_ciphertext=encrypt_str(
                    "old-state", firm_id=str(firm.id),
                ),
                expiration_date_time=expiry,
            )
        )
        await session.commit()

    # No HTTP calls expected.
    with respx.mock(assert_all_called=False):
        async with sm() as session, firm_context(firm.id):
            ctx = AppGraphContext(
                firm=firm, access_token="app-tok", session=session,
            )
            outcome = await ensure_subscription(
                session=session,
                ctx=ctx,
                user=user,
                resource=_resource(user),
                notification_url=_NOTIFICATION_URL,
                now=now,
            )

    assert outcome.action == "reused"
    assert outcome.row.subscription_id == "existing-sub"


async def test_renews_existing_row_when_near_expiry(bootstrap_env) -> None:
    sm = bootstrap_env["sm"]
    firm, user = await _seed(sm)
    bootstrap_env["created"].append(firm.id)

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    near_expiry = now + (DEFAULT_RENEWAL_BUFFER / 2)  # within the buffer
    new_expiry = now + DEFAULT_SUBSCRIPTION_TTL

    async with sm() as session, firm_context(firm.id):
        session.add(
            GraphSubscription(
                firm_id=firm.id,
                user_id=user.id,
                subscription_id="existing-sub",
                resource=_resource(user),
                notification_url=_NOTIFICATION_URL,
                change_type="created,updated",
                client_state_ciphertext=encrypt_str(
                    "old-state", firm_id=str(firm.id),
                ),
                expiration_date_time=near_expiry,
            )
        )
        await session.commit()

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.patch(f"{_SUBS_URL}/existing-sub").mock(
            return_value=httpx.Response(
                200,
                json=_subscription_response(
                    sub_id="existing-sub",
                    resource=_resource(user),
                    expiration=new_expiry,
                ),
            )
        )
        async with sm() as session, firm_context(firm.id):
            ctx = AppGraphContext(
                firm=firm, access_token="app-tok", session=session,
            )
            outcome = await ensure_subscription(
                session=session,
                ctx=ctx,
                user=user,
                resource=_resource(user),
                notification_url=_NOTIFICATION_URL,
                now=now,
            )
            await session.commit()

    assert outcome.action == "renewed"
    assert route.called

    async with sm() as session, firm_context(firm.id):
        row = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.subscription_id == "existing-sub")
            )
        ).scalar_one()
        # Use approx equality — Postgres may round microseconds.
        assert abs(
            (row.expiration_date_time - new_expiry).total_seconds()
        ) < 1
        assert row.last_renewed_at is not None
        # client_state was NOT rotated on renewal — same row, same secret.
        plaintext = decrypt_str(
            row.client_state_ciphertext, firm_id=str(firm.id),
        )
        assert plaintext == "old-state"


async def test_renew_404_falls_through_to_create(bootstrap_env) -> None:
    """If Graph 404s on renew, the stale row is deleted and a fresh
    subscription is created with a new client_state."""
    sm = bootstrap_env["sm"]
    firm, user = await _seed(sm)
    bootstrap_env["created"].append(firm.id)

    now = _dt.datetime(2026, 5, 14, 12, 0, tzinfo=_dt.UTC)
    near_expiry = now + (DEFAULT_RENEWAL_BUFFER / 2)
    new_expiry = now + DEFAULT_SUBSCRIPTION_TTL

    async with sm() as session, firm_context(firm.id):
        session.add(
            GraphSubscription(
                firm_id=firm.id,
                user_id=user.id,
                subscription_id="ghost-sub",
                resource=_resource(user),
                notification_url=_NOTIFICATION_URL,
                change_type="created,updated",
                client_state_ciphertext=encrypt_str(
                    "old-state", firm_id=str(firm.id),
                ),
                expiration_date_time=near_expiry,
            )
        )
        await session.commit()

    with respx.mock(assert_all_called=True) as rmock:
        rmock.patch(f"{_SUBS_URL}/ghost-sub").mock(
            return_value=httpx.Response(404, json={"error": "gone"})
        )
        rmock.post(_SUBS_URL).mock(
            return_value=httpx.Response(
                201,
                json=_subscription_response(
                    sub_id="fresh-replacement",
                    resource=_resource(user),
                    expiration=new_expiry,
                ),
            )
        )

        async with sm() as session, firm_context(firm.id):
            ctx = AppGraphContext(
                firm=firm, access_token="app-tok", session=session,
            )
            outcome = await ensure_subscription(
                session=session,
                ctx=ctx,
                user=user,
                resource=_resource(user),
                notification_url=_NOTIFICATION_URL,
                now=now,
                client_state_factory=lambda: "rotated-state",
            )
            await session.commit()

    assert outcome.action == "created"
    assert outcome.row.subscription_id == "fresh-replacement"

    async with sm() as session, firm_context(firm.id):
        rows = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.firm_id == firm.id)
            )
        ).scalars().all()
        # Stale ghost row was deleted; only the replacement remains.
        assert len(rows) == 1
        assert rows[0].subscription_id == "fresh-replacement"
        plaintext = decrypt_str(
            rows[0].client_state_ciphertext, firm_id=str(firm.id),
        )
        assert plaintext == "rotated-state"


async def test_rejects_user_from_other_firm(bootstrap_env) -> None:
    sm = bootstrap_env["sm"]
    firm_a, _ = await _seed(sm)
    bootstrap_env["created"].append(firm_a.id)
    firm_b, user_b = await _seed(sm)
    bootstrap_env["created"].append(firm_b.id)

    async with sm() as session, firm_context(firm_a.id):
        ctx = AppGraphContext(
            firm=firm_a, access_token="app-tok", session=session,
        )
        with pytest.raises(ValueError, match="firm_id"):
            await ensure_subscription(
                session=session,
                ctx=ctx,
                user=user_b,  # belongs to a different firm
                resource=_resource(user_b),
                notification_url=_NOTIFICATION_URL,
            )
