"""Integration tests for `graph_context` FastAPI dependency.

End-to-end through TestClient: real DB (test instance), real session
JWT, Microsoft's token endpoint mocked via respx where refresh paths
are exercised. A stub ``/graph-test/whoami`` route consumes
``Depends(graph_context)`` so the dependency is exercised exactly as
a real Graph route would exercise it.

The dependency's job has two halves:

1. Resolve the authenticated user (delegated to ``current_user``,
   covered exhaustively in ``test_current_user.py``).
2. Decide whether to refresh the access token, then yield a bundle.

These tests focus on (2). The refresh-decision matrix has five
inputs that matter — far-from-expiry, near-expiry, already-expired,
NULL expiry, NULL access ciphertext — plus the failure path where
Microsoft rejects the refresh.
"""
import asyncio
import datetime as _dt
import uuid

import httpx
import jwt
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.config import get_settings
from coworker.connectors.exceptions import ConnectorAuthError
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.context import GraphContext, graph_context
from coworker.security.encryption import encrypt_str

# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def graph_context_environment(test_database_url, monkeypatch):
    """Mini FastAPI app with /graph-test/whoami protected by graph_context."""
    test_engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(test_engine)
    test_sm = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    from coworker.db import session as session_module

    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: test_sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: test_engine)

    app = FastAPI()

    @app.get("/graph-test/whoami")
    async def whoami(
        ctx: GraphContext = Depends(graph_context),
    ) -> dict[str, str]:
        return {
            "user_id": str(ctx.user.id),
            "firm_id": str(ctx.firm.id),
            "access_token": ctx.access_token,
        }

    client = TestClient(app, follow_redirects=False)

    created_firm_ids: list[uuid.UUID] = []
    try:
        yield {
            "client": client,
            "sessionmaker": test_sm,
            "created_firm_ids": created_firm_ids,
        }
    finally:
        for firm_id in created_firm_ids:
            asyncio.run(_delete_test_firm(test_sm, firm_id))
        asyncio.run(test_engine.dispose())


async def _delete_test_firm(sessionmaker, firm_id: uuid.UUID) -> None:
    """Drop firm/user/audit rows under NO FORCE bracket."""
    _TABLES = ("firms", "users", "audit_log")
    async with sessionmaker() as session:
        for t in _TABLES:
            await session.execute(text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
        try:
            await session.execute(
                text("DELETE FROM audit_log WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM users WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"), {"id": str(firm_id)}
            )
            await session.commit()
        finally:
            for t in _TABLES:
                await session.execute(
                    text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
                )
            await session.commit()


def _seed(
    sessionmaker,
    *,
    slug: str,
    upn: str,
    tenant_id: str,
    client_id: str,
    client_secret_plain: str,
    refresh_token_plain: str,
    access_token_plain: str | None,
    expires_in_minutes: float | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a firm + user with controllable token expiry.

    ``access_token_plain=None`` ⇒ ``ms_access_token_ciphertext`` is NULL.
    ``expires_in_minutes=None`` ⇒ ``ms_token_expires_at`` is NULL.
    """

    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Graph Context Test Firm",
                    slug=slug,
                    azure_tenant_id=tenant_id,
                    azure_client_id=client_id,
                    azure_client_secret_ciphertext=encrypt_str(
                        client_secret_plain, firm_id=firm_id_str
                    ),
                )
            )
            await session.flush()

            access_ct = (
                encrypt_str(access_token_plain, firm_id=firm_id_str)
                if access_token_plain is not None
                else None
            )
            expires_at = (
                _dt.datetime.now(_dt.UTC)
                + _dt.timedelta(minutes=expires_in_minutes)
                if expires_in_minutes is not None
                else None
            )

            user = User(
                firm_id=firm_id,
                azure_object_id=uuid.uuid4().hex,
                upn=upn,
                display_name="Graph Context Test User",
                ms_access_token_ciphertext=access_ct,
                ms_refresh_token_ciphertext=encrypt_str(
                    refresh_token_plain, firm_id=firm_id_str
                ),
                ms_token_expires_at=expires_at,
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            await session.commit()
            return firm_id, user_id

    return asyncio.run(_run())


def _issue_jwt(*, user_id: uuid.UUID, firm_id: uuid.UUID) -> str:
    """JWT signed with the configured SESSION_JWT_SECRET, 5-min TTL."""
    now = _dt.datetime.now(_dt.UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "firm_id": str(firm_id),
            "iat": int(now.timestamp()),
            "exp": int((now + _dt.timedelta(seconds=300)).timestamp()),
        },
        get_settings().SESSION_JWT_SECRET.get_secret_value(),
        algorithm="HS256",
    )


# --------------------------- happy paths ------------------------------------


def test_far_from_expiry_returns_existing_token_no_refresh(
    graph_context_environment,
) -> None:
    """ms_token_expires_at far from now ⇒ no refresh, return existing token.

    No respx mock: any outbound HTTP attempt would hit the real
    Microsoft endpoint (or fail offline). The fact that the test
    completes immediately and returns the seeded token proves no
    refresh fired.
    """
    sm = graph_context_environment["sessionmaker"]
    client = graph_context_environment["client"]
    created = graph_context_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm,
        slug=f"gc-far-{uuid.uuid4().hex[:8]}",
        upn=f"alice-{uuid.uuid4().hex[:8]}@example.com",
        tenant_id=str(uuid.uuid4()),
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-secret",
        refresh_token_plain="refresh-tok",
        access_token_plain="existing-access-token",
        expires_in_minutes=55,  # well outside the 5-minute buffer
    )
    created.append(firm_id)

    token = _issue_jwt(user_id=user_id, firm_id=firm_id)
    response = client.get(
        "/graph-test/whoami", cookies={"coworker_session": token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user_id)
    assert body["firm_id"] == str(firm_id)
    assert body["access_token"] == "existing-access-token"


def test_within_buffer_triggers_refresh(graph_context_environment) -> None:
    """ms_token_expires_at within the 5-minute buffer ⇒ refresh fires."""
    sm = graph_context_environment["sessionmaker"]
    client = graph_context_environment["client"]
    created = graph_context_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed(
        sm,
        slug=f"gc-buf-{uuid.uuid4().hex[:8]}",
        upn=f"bob-{uuid.uuid4().hex[:8]}@example.com",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-secret",
        refresh_token_plain="refresh-tok",
        access_token_plain="old-access-token",
        expires_in_minutes=2,  # inside the buffer
    )
    created.append(firm_id)

    token = _issue_jwt(user_id=user_id, firm_id=firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "fresh-access-token",
                    "refresh_token": "rotated-refresh-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        response = client.get(
            "/graph-test/whoami", cookies={"coworker_session": token}
        )

    assert response.status_code == 200
    assert response.json()["access_token"] == "fresh-access-token"


def test_already_expired_triggers_refresh(graph_context_environment) -> None:
    sm = graph_context_environment["sessionmaker"]
    client = graph_context_environment["client"]
    created = graph_context_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed(
        sm,
        slug=f"gc-exp-{uuid.uuid4().hex[:8]}",
        upn=f"carol-{uuid.uuid4().hex[:8]}@example.com",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-secret",
        refresh_token_plain="refresh-tok",
        access_token_plain="dead-access-token",
        expires_in_minutes=-10,
    )
    created.append(firm_id)

    token = _issue_jwt(user_id=user_id, firm_id=firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "revived-access-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        response = client.get(
            "/graph-test/whoami", cookies={"coworker_session": token}
        )

    assert response.status_code == 200
    assert response.json()["access_token"] == "revived-access-token"


def test_null_expires_at_triggers_refresh(graph_context_environment) -> None:
    """A NULL ``ms_token_expires_at`` (incomplete onboarding) forces a refresh."""
    sm = graph_context_environment["sessionmaker"]
    client = graph_context_environment["client"]
    created = graph_context_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed(
        sm,
        slug=f"gc-nullexp-{uuid.uuid4().hex[:8]}",
        upn=f"dave-{uuid.uuid4().hex[:8]}@example.com",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-secret",
        refresh_token_plain="refresh-tok",
        access_token_plain="any-access-token",
        expires_in_minutes=None,
    )
    created.append(firm_id)

    token = _issue_jwt(user_id=user_id, firm_id=firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "fresh-after-null",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        response = client.get(
            "/graph-test/whoami", cookies={"coworker_session": token}
        )

    assert response.status_code == 200
    assert response.json()["access_token"] == "fresh-after-null"


def test_null_access_ciphertext_triggers_refresh(
    graph_context_environment,
) -> None:
    """A user row with NULL ``ms_access_token_ciphertext`` but a valid
    ``ms_token_expires_at`` is inconsistent and forces a refresh.

    This shouldn't happen in normal onboarding flow but defends against
    a partially-rolled-back migration or a half-applied admin fix.
    """
    sm = graph_context_environment["sessionmaker"]
    client = graph_context_environment["client"]
    created = graph_context_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed(
        sm,
        slug=f"gc-noct-{uuid.uuid4().hex[:8]}",
        upn=f"eve-{uuid.uuid4().hex[:8]}@example.com",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-secret",
        refresh_token_plain="refresh-tok",
        access_token_plain=None,
        expires_in_minutes=55,  # far from expiry — but ciphertext is NULL
    )
    created.append(firm_id)

    token = _issue_jwt(user_id=user_id, firm_id=firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "repaired-access-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        response = client.get(
            "/graph-test/whoami", cookies={"coworker_session": token}
        )

    assert response.status_code == 200
    assert response.json()["access_token"] == "repaired-access-token"


# --------------------------- failure paths ----------------------------------


def test_refresh_4xx_propagates_connector_auth_error(
    graph_context_environment,
) -> None:
    """A 4xx from Microsoft (revoked refresh token, etc.) raises
    ``ConnectorAuthError`` from ``refresh_access_token``. The dependency
    must NOT catch it.

    Asserting the exception type is more honest than asserting a 500
    status code: this captures the dependency's contract directly.
    Phase 12 will install a custom exception handler that maps
    ``ConnectorAuthError`` to 401 with a sign-in-again hint; that
    handler lives at the app boundary, not in the dependency, so this
    test will continue to pass unchanged when that handler lands.
    """
    sm = graph_context_environment["sessionmaker"]
    client = graph_context_environment["client"]
    created = graph_context_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed(
        sm,
        slug=f"gc-4xx-{uuid.uuid4().hex[:8]}",
        upn=f"frank-{uuid.uuid4().hex[:8]}@example.com",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-secret",
        refresh_token_plain="revoked-refresh",
        access_token_plain="x",
        expires_in_minutes=-1,  # forces refresh
    )
    created.append(firm_id)

    token = _issue_jwt(user_id=user_id, firm_id=firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        ).mock(
            return_value=httpx.Response(
                400,
                json={"error": "invalid_grant"},
            )
        )
        with pytest.raises(ConnectorAuthError):
            client.get(
                "/graph-test/whoami", cookies={"coworker_session": token}
            )


# --------------------------- representation safety --------------------------


def test_graph_context_repr_redacts_access_token() -> None:
    """``repr(GraphContext)`` must never include the access token.

    Pure-Python test: instantiate directly with stub objects, verify
    string. Belt-and-braces with the loguru patcher's redaction pass.
    """

    class _Stub:
        def __init__(self, x: str) -> None:
            self.id = x

    ctx = GraphContext(
        firm=_Stub("firm-id-here"),  # type: ignore[arg-type]
        user=_Stub("user-id-here"),  # type: ignore[arg-type]
        access_token="super-secret-token-do-not-leak",
        session=None,  # type: ignore[arg-type]
    )
    rendered = repr(ctx)
    assert "super-secret-token-do-not-leak" not in rendered
    assert "redacted" in rendered.lower()
