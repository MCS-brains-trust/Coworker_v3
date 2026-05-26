"""Integration tests for the OAuth routes.

End-to-end through FastAPI's TestClient — the routes run real DB and
real Redis (against test instances), and Microsoft's token endpoint
is mocked via respx because actually hitting login.microsoftonline.com
in CI would require a real Azure app and a real consenting user.

Test app
--------
Step 5 (next commit) is the "mount the auth router on coworker.api.main"
step. Until then, tests build a minimal FastAPI app that includes the
router so we can exercise it without modifying main.py prematurely.

DB redirection
--------------
The routes call `Depends(get_session)` which calls `get_sessionmaker()`.
The fixture monkey-patches `get_sessionmaker` to return a sessionmaker
bound to the test database. Same shape as `test_cli_create_firm.py`.

Redis redirection
-----------------
oauth_state.py and the route module both `from coworker.db.redis import
get_redis`, so we patch BOTH module namespaces (definition site AND
import sites). The patched `get_redis` returns a *fresh* client on each
call rather than one shared client — TestClient runs the app in its own
event loop and verification runs in a separate `asyncio.run()`, so a
single shared client breaks across loops. Connections leak for the
test duration; cleanup is a single `flushdb` at fixture teardown.
"""
import asyncio
import base64
import json
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
import respx
import httpx
import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from coworker.api.routes import auth as auth_module
from coworker.cli.main import _bootstrap_firm
from coworker.config import get_settings
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import decrypt_str
from coworker.security.oauth_state import create_state
from coworker.security.session import decode_session_jwt


_TEST_REDIS_DB = "/15"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return parsed._replace(path=_TEST_REDIS_DB).geturl()


def _make_unsigned_jwt(claims: dict) -> str:
    """Construct an alg=none JWT for test fixtures.

    PyJWT's encode-with-alg=none has historically been finicky across
    versions. Building the three segments by hand is bulletproof and
    matches exactly what `decode_id_token_unverified` consumes.
    """
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    return f"{header}.{payload}."


@pytest.fixture
def auth_test_environment(test_database_url, monkeypatch):
    """One fixture, three patches: sessionmaker, get_redis (two sites),
    and a fresh TestClient against an app with the auth router mounted.
    """
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

    test_redis_url = _test_redis_url()

    def fresh_redis_client():
        from redis.asyncio import from_url
        return from_url(test_redis_url, encoding="utf-8", decode_responses=True)

    from coworker.db import redis as redis_module
    from coworker.security import oauth_state as oauth_state_module

    monkeypatch.setattr(redis_module, "get_redis", fresh_redis_client)
    monkeypatch.setattr(oauth_state_module, "get_redis", fresh_redis_client)
    # The route imports create_state/consume_state but not get_redis itself;
    # those functions resolve get_redis at call time via oauth_state_module's
    # namespace, which we patched above.

    # Build a mini-app with just the auth router for testing.
    app = FastAPI()
    app.include_router(auth_module.router)
    client = TestClient(app, follow_redirects=False)

    async def _flush_redis() -> None:
        c = fresh_redis_client()
        await c.flushdb()
        await c.aclose()

    asyncio.run(_flush_redis())

    try:
        yield {
            "client": client,
            "sessionmaker": test_sm,
            "redis_url": test_redis_url,
            "fresh_redis": fresh_redis_client,
        }
    finally:
        asyncio.run(_flush_redis())
        asyncio.run(test_engine.dispose())


def _bootstrap_test_firm(
    sessionmaker, *, slug: str, tenant_id: str, client_id: str, secret: str
) -> uuid.UUID:
    """Provision a firm in the test DB; returns its id."""
    async def _run() -> uuid.UUID:
        async with sessionmaker() as session:
            firm_id = await _bootstrap_firm(
                session,
                slug=slug,
                name="Auth Route Test Firm",
                azure_tenant_id=tenant_id,
                azure_client_id=client_id,
                azure_client_secret=secret,
            )
            await session.commit()
            return firm_id
    return asyncio.run(_run())


def _delete_test_firm(sessionmaker, firm_id: uuid.UUID) -> None:
    """Cleanup helper. Lifts FORCE on firms / users / audit_log so we
    can DELETE the rows the test created. audit_log.firm_id has
    ON DELETE RESTRICT (Stage C2 issue I) so we must DELETE audit
    entries before the firm row, otherwise the firm DELETE fails with
    an FK violation and aborts the transaction.
    """
    from sqlalchemy import text

    _TABLES = ("firms", "users", "audit_log")

    async def _run() -> None:
        async with sessionmaker() as session:
            for t in _TABLES:
                await session.execute(
                    text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
                )
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
                    text("DELETE FROM firms WHERE id = :id"),
                    {"id": str(firm_id)},
                )
                await session.commit()
            finally:
                for t in _TABLES:
                    await session.execute(
                        text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
                    )
                await session.commit()
    asyncio.run(_run())


# ----------------------------- /start tests ---------------------------------


def test_start_unknown_slug_returns_404(auth_test_environment) -> None:
    client = auth_test_environment["client"]
    slug = f"nonexistent-{uuid.uuid4().hex[:8]}"
    response = client.get(f"/api/v1/auth/microsoft/start/{slug}")
    assert response.status_code == 404


def test_start_known_slug_redirects_to_microsoft(auth_test_environment) -> None:
    sessionmaker = auth_test_environment["sessionmaker"]
    client = auth_test_environment["client"]

    slug = f"start-test-{uuid.uuid4().hex[:8]}"
    tenant_id = str(uuid.uuid4())
    client_id = str(uuid.uuid4())
    firm_id = _bootstrap_test_firm(
        sessionmaker,
        slug=slug,
        tenant_id=tenant_id,
        client_id=client_id,
        secret="initial-secret",
    )
    try:
        response = client.get(f"/api/v1/auth/microsoft/start/{slug}")
        assert response.status_code == 302
        location = response.headers["location"]
        parsed = urlparse(location)
        assert parsed.netloc == "login.microsoftonline.com"
        assert parsed.path == f"/{tenant_id}/oauth2/v2.0/authorize"
        params = parse_qs(parsed.query)
        assert params["client_id"] == [client_id]
        assert params["response_type"] == ["code"]
        assert params["code_challenge_method"] == ["S256"]
        assert "state" in params
        assert "code_challenge" in params
    finally:
        _delete_test_firm(sessionmaker, firm_id)


# ---------------------------- /callback tests -------------------------------


def test_callback_invalid_state_returns_generic_400(auth_test_environment) -> None:
    client = auth_test_environment["client"]
    response = client.get(
        "/api/v1/auth/microsoft/callback",
        params={"code": "anything", "state": "never-issued"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "authentication failed"}


def test_callback_success_creates_user_with_encrypted_tokens(
    auth_test_environment,
) -> None:
    sessionmaker = auth_test_environment["sessionmaker"]
    client = auth_test_environment["client"]

    slug = f"callback-success-{uuid.uuid4().hex[:8]}"
    tenant_id = str(uuid.uuid4())
    client_id = str(uuid.uuid4())
    firm_id = _bootstrap_test_firm(
        sessionmaker,
        slug=slug,
        tenant_id=tenant_id,
        client_id=client_id,
        secret="firm-client-secret",
    )

    try:
        # Pre-seed a state in Redis (skip /start, which would also work).
        state_token, code_verifier = asyncio.run(create_state(firm_id))

        oid = uuid.uuid4().hex
        upn = f"alice-{uuid.uuid4().hex[:8]}@example.com"
        fake_id_token = _make_unsigned_jwt(
            {"oid": oid, "preferred_username": upn, "name": "Alice Example"}
        )

        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "fake-access-token",
                        "refresh_token": "fake-refresh-token",
                        "id_token": fake_id_token,
                        "expires_in": 3600,
                        "token_type": "Bearer",
                        "scope": "User.Read offline_access",
                    },
                )
            )

            response = client.get(
                "/api/v1/auth/microsoft/callback",
                params={"code": "fake-auth-code", "state": state_token},
            )

        # Redirect to OAUTH_POST_LOGIN_REDIRECT (default "/")
        assert response.status_code == 302
        assert response.headers["location"] == get_settings().OAUTH_POST_LOGIN_REDIRECT

        # Session cookie set
        cookie = response.cookies.get("coworker_session")
        assert cookie is not None
        claims = decode_session_jwt(cookie)
        assert claims["firm_id"] == str(firm_id)
        assert claims["sub"]
        assert claims["exp"] > claims["iat"]

        # User row persisted with encrypted tokens
        async def _verify_user_row() -> None:
            async with sessionmaker() as session, firm_context(firm_id):
                user = (
                    await session.execute(
                        select(User).where(User.azure_object_id == oid)
                    )
                ).scalar_one()
                assert user.firm_id == firm_id
                assert user.upn == upn
                assert user.display_name == "Alice Example"
                assert user.ms_token_expires_at is not None

                # Both tokens decrypt under the firm's AAD
                assert (
                    decrypt_str(user.ms_access_token_ciphertext, firm_id=str(firm_id))
                    == "fake-access-token"
                )
                assert (
                    decrypt_str(user.ms_refresh_token_ciphertext, firm_id=str(firm_id))
                    == "fake-refresh-token"
                )
                # And NOT under a different firm's AAD
                from cryptography.exceptions import InvalidTag
                with pytest.raises(InvalidTag):
                    decrypt_str(
                        user.ms_refresh_token_ciphertext,
                        firm_id=str(uuid.uuid4()),
                    )

        asyncio.run(_verify_user_row())
    finally:
        _delete_test_firm(sessionmaker, firm_id)


def test_callback_session_cookie_attributes_in_dev(auth_test_environment) -> None:
    """ENVIRONMENT defaults to "dev"; assert cookie has HttpOnly + SameSite=Lax
    + Secure=False + Path=/ in dev. The Secure=True case under non-dev
    ENVIRONMENT is verified by code review of routes/auth.py — switching
    Settings mid-test is brittle and the conditional is one line."""
    sessionmaker = auth_test_environment["sessionmaker"]
    client = auth_test_environment["client"]

    slug = f"cookie-test-{uuid.uuid4().hex[:8]}"
    tenant_id = str(uuid.uuid4())
    client_id = str(uuid.uuid4())
    firm_id = _bootstrap_test_firm(
        sessionmaker,
        slug=slug,
        tenant_id=tenant_id,
        client_id=client_id,
        secret="firm-client-secret",
    )

    try:
        state_token, _ = asyncio.run(create_state(firm_id))
        fake_id_token = _make_unsigned_jwt(
            {
                "oid": uuid.uuid4().hex,
                "preferred_username": f"bob-{uuid.uuid4().hex[:8]}@example.com",
                "name": "Bob",
            }
        )

        with respx.mock() as rmock:
            rmock.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "a",
                        "refresh_token": "r",
                        "id_token": fake_id_token,
                        "expires_in": 3600,
                    },
                )
            )

            response = client.get(
                "/api/v1/auth/microsoft/callback",
                params={"code": "c", "state": state_token},
            )

        # Inspect Set-Cookie header attributes directly — TestClient's
        # parsed cookies don't always preserve flag attributes.
        set_cookie_header = response.headers["set-cookie"]
        assert "coworker_session=" in set_cookie_header
        assert "HttpOnly" in set_cookie_header
        assert "SameSite=lax" in set_cookie_header.lower().replace(" ", "") or \
               "samesite=lax" in set_cookie_header.lower()
        assert "Path=/" in set_cookie_header
        # Dev mode: Secure flag must NOT be set
        assert "Secure" not in set_cookie_header
    finally:
        _delete_test_firm(sessionmaker, firm_id)
