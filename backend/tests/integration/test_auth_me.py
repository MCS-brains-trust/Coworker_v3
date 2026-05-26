"""Integration tests for ``GET /api/v1/auth/me``.

The Phase 10-2 frontend hits this on mount to distinguish
"signed in" from "401, redirect to login." Same generic 401
response as the other auth-required endpoints when the cookie
is missing / forged / expired.
"""
import datetime as _dt
import uuid

import jwt
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.api.main import app
from coworker.config import get_settings
from coworker.db.models import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest_asyncio.fixture
async def me_env(test_database_url, monkeypatch):
    from coworker.db import session as session_module

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)

    firm_id = uuid.uuid4()
    slug = f"me-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        firm = Firm(id=firm_id, name="Me Firm", slug=slug)
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Principal Pal",
            role="principal",
        )
        session.add_all([firm, user])
        await session.commit()
        user_id = user.id

    try:
        yield {
            "sm": sm, "firm_id": firm_id, "user_id": user_id, "slug": slug,
        }
    finally:
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


def _issue_jwt(*, user_id: uuid.UUID, firm_id: uuid.UUID) -> str:
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


def test_me_returns_user_when_authenticated(me_env) -> None:
    firm_id = me_env["firm_id"]
    user_id = me_env["user_id"]
    slug = me_env["slug"]

    client = TestClient(app)
    client.cookies.set(
        "coworker_session",
        _issue_jwt(user_id=user_id, firm_id=firm_id),
    )
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(user_id)
    assert body["firm_id"] == str(firm_id)
    assert body["firm_slug"] == slug
    assert body["display_name"] == "Principal Pal"
    assert body["role"] == "principal"
    assert body["upn"].endswith("@example.com")


def test_me_without_cookie_returns_401(me_env) -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    # Generic body — same as every other 401 the deps issue.
    assert resp.json() == {"detail": "authentication required"}


def test_me_with_bad_signature_returns_401(me_env) -> None:
    firm_id = me_env["firm_id"]
    user_id = me_env["user_id"]
    bad = jwt.encode(
        {
            "sub": str(user_id),
            "firm_id": str(firm_id),
            "iat": 0, "exp": 9999999999,
        },
        "wrong-secret",
        algorithm="HS256",
    )
    client = TestClient(app)
    client.cookies.set("coworker_session", bad)
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
