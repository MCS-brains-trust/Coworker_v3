"""End-to-end tests for /api/v1/conversations.

Same FastAPI TestClient + real-DB pattern as
``test_specialist_routes.py``. Each test seeds a firm + a user, hits
the routes, and cleans up the inserted rows in teardown so the test
DB is reusable across runs.

The send-message route is not exercised here (it opens an Anthropic
stream); orchestrator-level behaviour is covered in
``test_chat_orchestrator.py`` with a stubbed client. This file
focuses on routing, response shapes, and RLS isolation.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from collections.abc import AsyncIterator

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
from coworker.db.models import (
    ChatConversation,
    Firm,
    User,
)
from coworker.db.session import _attach_pool_listeners, firm_context

_FORCED_RLS_TABLES = (
    "firms",
    "users",
    "audit_log",
    "chat_conversations",
    "chat_messages",
)


@pytest_asyncio.fixture
async def routes_env(test_database_url, monkeypatch) -> AsyncIterator[dict]:
    from coworker.db import session as session_module

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)

    firm_id = uuid.uuid4()
    slug = f"chat-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Chat Firm", slug=slug))
        await session.commit()

    try:
        yield {"sm": sm, "firm_id": firm_id, "slug": slug}
    finally:
        await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id: uuid.UUID) -> None:
    async with sm() as session:
        for t in _FORCED_RLS_TABLES:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            # Order matters: child tables before parents.
            for t in (
                "chat_messages",
                "chat_conversations",
                "audit_log",
                "users",
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
        for t in _FORCED_RLS_TABLES:
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


def _client(*, user_id: uuid.UUID, firm_id: uuid.UUID) -> TestClient:
    client = TestClient(app)
    client.cookies.set(
        "coworker_session", _issue_jwt(user_id=user_id, firm_id=firm_id)
    )
    return client


async def _seed_user(sm, firm_id: uuid.UUID) -> uuid.UUID:
    async with sm() as session, firm_context(firm_id):
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Chat User",
            role="accountant",
        )
        session.add(user)
        await session.commit()
        return user.id


async def _seed_conversation(
    sm,
    firm_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    title: str | None = None,
    updated_at: _dt.datetime | None = None,
) -> uuid.UUID:
    async with sm() as session, firm_context(firm_id):
        conv = ChatConversation(
            firm_id=firm_id, user_id=user_id, title=title
        )
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
        if updated_at is not None:
            conv.updated_at = updated_at
            await session.commit()
        return conv.id


# ===========================================================================
# POST /api/v1/conversations
# ===========================================================================


def test_create_conversation_returns_201_with_id(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    client = _client(user_id=user_id, firm_id=firm_id)

    resp = client.post("/api/v1/conversations", json={})
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["id"])
    assert body["title"] is None
    assert body["created_at"] is not None
    assert body["updated_at"] is not None


def test_create_conversation_accepts_title(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    client = _client(user_id=user_id, firm_id=firm_id)

    resp = client.post(
        "/api/v1/conversations", json={"title": "First chat"}
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == "First chat"


# ===========================================================================
# GET /api/v1/conversations
# ===========================================================================


def test_list_conversations_returns_user_conversations_sorted_desc(
    routes_env,
) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))

    # Three conversations with controlled updated_at timestamps.
    now = _dt.datetime.now(_dt.UTC)
    oldest = asyncio.run(
        _seed_conversation(
            sm, firm_id, user_id,
            title="oldest", updated_at=now - _dt.timedelta(hours=2),
        )
    )
    middle = asyncio.run(
        _seed_conversation(
            sm, firm_id, user_id,
            title="middle", updated_at=now - _dt.timedelta(hours=1),
        )
    )
    newest = asyncio.run(
        _seed_conversation(
            sm, firm_id, user_id,
            title="newest", updated_at=now,
        )
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/conversations")
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()["conversations"]]
    assert ids == [str(newest), str(middle), str(oldest)]


def test_list_conversations_empty(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/conversations")
    assert resp.status_code == 200
    assert resp.json() == {"conversations": []}


# ===========================================================================
# GET /api/v1/conversations/{id}/messages
# ===========================================================================


def test_get_message_history_empty_for_fresh_conversation(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    conv_id = asyncio.run(_seed_conversation(sm, firm_id, user_id))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get(f"/api/v1/conversations/{conv_id}/messages")
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


def test_get_message_history_404_unknown_id(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    client = _client(user_id=user_id, firm_id=firm_id)

    resp = client.get(f"/api/v1/conversations/{uuid.uuid4()}/messages")
    assert resp.status_code == 404


# ===========================================================================
# POST /api/v1/conversations/{id}/messages (404 only — happy path is
# covered by the orchestrator tests; running the SSE handler here would
# require either a live Anthropic call or a non-trivial app-level mock.)
# ===========================================================================


def test_send_message_404_unknown_id(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    client = _client(user_id=user_id, firm_id=firm_id)

    resp = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages",
        json={"content": "Hello"},
    )
    assert resp.status_code == 404


def test_send_message_validation_rejects_empty_content(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    conv_id = asyncio.run(_seed_conversation(sm, firm_id, user_id))
    client = _client(user_id=user_id, firm_id=firm_id)

    resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"content": ""},
    )
    assert resp.status_code == 422


# ===========================================================================
# Cross-firm isolation (RLS)
# ===========================================================================


def test_cross_firm_isolation_list_conversations(routes_env) -> None:
    """Firm A creates a conversation; firm B's list endpoint returns
    nothing — RLS hides cross-firm rows."""
    sm = routes_env["sm"]
    firm_a_id = routes_env["firm_id"]
    user_a_id = asyncio.run(_seed_user(sm, firm_a_id))
    asyncio.run(
        _seed_conversation(sm, firm_a_id, user_a_id, title="firm A's chat")
    )

    firm_b_id = uuid.uuid4()
    user_b_id = uuid.uuid4()

    async def _seed_other_firm() -> None:
        async with sm() as session, firm_context(firm_b_id):
            session.add(Firm(
                id=firm_b_id, name="Other Firm",
                slug=f"o-{uuid.uuid4().hex[:8]}",
            ))
            session.add(User(
                id=user_b_id, firm_id=firm_b_id,
                azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
                upn=f"o-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Other",
                role="accountant",
            ))
            await session.commit()

    asyncio.run(_seed_other_firm())

    try:
        client = _client(user_id=user_b_id, firm_id=firm_b_id)
        resp = client.get("/api/v1/conversations")
        assert resp.status_code == 200
        assert resp.json() == {"conversations": []}
    finally:
        asyncio.run(_cleanup_firm(sm, firm_b_id))


def test_cross_firm_isolation_get_history(routes_env) -> None:
    """Firm B cannot read firm A's conversation history — 404 via RLS."""
    sm = routes_env["sm"]
    firm_a_id = routes_env["firm_id"]
    user_a_id = asyncio.run(_seed_user(sm, firm_a_id))
    conv_id = asyncio.run(_seed_conversation(sm, firm_a_id, user_a_id))

    firm_b_id = uuid.uuid4()
    user_b_id = uuid.uuid4()

    async def _seed_other_firm() -> None:
        async with sm() as session, firm_context(firm_b_id):
            session.add(Firm(
                id=firm_b_id, name="Other Firm",
                slug=f"o-{uuid.uuid4().hex[:8]}",
            ))
            session.add(User(
                id=user_b_id, firm_id=firm_b_id,
                azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
                upn=f"o-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Other",
                role="accountant",
            ))
            await session.commit()

    asyncio.run(_seed_other_firm())

    try:
        client = _client(user_id=user_b_id, firm_id=firm_b_id)
        resp = client.get(f"/api/v1/conversations/{conv_id}/messages")
        assert resp.status_code == 404
    finally:
        asyncio.run(_cleanup_firm(sm, firm_b_id))
