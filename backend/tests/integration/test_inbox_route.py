"""Integration tests for `GET /api/inbox`.

End-to-end through TestClient: real DB, real session JWT,
Microsoft Graph mocked via respx. Pattern mirrors
`test_graph_context.py` — same DB redirection, same firm/user
seeding helpers — but the assertions are about the route surface
(status code, JSON shape, ``top`` parameter forwarding) rather than
the underlying dependency.

The matrix of refresh-decision branches and the connector failure
taxonomy are covered exhaustively in `test_graph_context.py` and
`test_graph_mail.py` respectively. This file does not duplicate them.
"""
import asyncio
import datetime as _dt
import uuid

import httpx
import jwt
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from coworker.api.main import app
from coworker.config import get_settings
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import encrypt_str

_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def inbox_route_environment(test_database_url, monkeypatch):
    """TestClient against the real `app` with DB redirected to test instance."""
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
    tables = ("firms", "users", "audit_log")
    async with sessionmaker() as session:
        for t in tables:
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
            for t in tables:
                await session.execute(
                    text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
                )
            await session.commit()


def _seed(sessionmaker, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a firm + user with a far-from-expiry access token."""

    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Inbox Route Test Firm",
                    slug=slug,
                    azure_tenant_id=str(uuid.uuid4()),
                    azure_client_id=str(uuid.uuid4()),
                    azure_client_secret_ciphertext=encrypt_str(
                        "secret", firm_id=firm_id_str
                    ),
                )
            )
            await session.flush()
            user = User(
                firm_id=firm_id,
                azure_object_id=uuid.uuid4().hex,
                upn=f"inbox-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Inbox Route Test User",
                ms_access_token_ciphertext=encrypt_str(
                    "live-access-token", firm_id=firm_id_str
                ),
                ms_refresh_token_ciphertext=encrypt_str(
                    "refresh-token", firm_id=firm_id_str
                ),
                ms_token_expires_at=_dt.datetime.now(_dt.UTC)
                + _dt.timedelta(hours=1),
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            await session.commit()
            return firm_id, user_id

    return asyncio.run(_run())


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


def _graph_message(idx: int) -> dict:
    return {
        "id": f"msg-{idx}",
        "subject": f"Subject {idx}",
        "from": {
            "emailAddress": {
                "address": f"sender{idx}@example.com",
                "name": f"Sender {idx}",
            }
        },
        "receivedDateTime": f"2026-05-08T{10 + idx % 12:02d}:00:00Z",
        "bodyPreview": f"Preview {idx}",
        "isRead": idx % 2 == 0,
        "hasAttachments": idx == 0,
    }


# --------------------------- happy path -------------------------------------


def test_inbox_returns_25_messages_for_signed_in_user(
    inbox_route_environment,
) -> None:
    sm = inbox_route_environment["sessionmaker"]
    client = inbox_route_environment["client"]
    created = inbox_route_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"inbox-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.get(_GRAPH_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={"value": [_graph_message(i) for i in range(25)]},
            )
        )
        response = client.get(
            "/api/inbox",
            cookies={
                "coworker_session": _issue_jwt(
                    user_id=user_id, firm_id=firm_id
                )
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 25
    assert body[0]["id"] == "msg-0"
    assert body[0]["subject"] == "Subject 0"
    assert body[0]["sender"] == {
        "email": "sender0@example.com",
        "name": "Sender 0",
    }


def test_inbox_top_parameter_is_forwarded(inbox_route_environment) -> None:
    sm = inbox_route_environment["sessionmaker"]
    client = inbox_route_environment["client"]
    created = inbox_route_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"inbox-top-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.get(_GRAPH_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={"value": [_graph_message(i) for i in range(5)]},
            )
        )
        response = client.get(
            "/api/inbox?top=5",
            cookies={
                "coworker_session": _issue_jwt(
                    user_id=user_id, firm_id=firm_id
                )
            },
        )

    assert response.status_code == 200
    assert len(response.json()) == 5
    sent = route.calls.last.request
    assert sent.url.params["$top"] == "5"


# --------------------------- input validation -------------------------------


def test_inbox_rejects_invalid_top_with_422(inbox_route_environment) -> None:
    """FastAPI's Query validation rejects top outside [1, 1000].

    Auth is provided because FastAPI runs dependencies (including
    `current_user`) before query validation; without auth we'd get
    401 instead of 422.
    """
    sm = inbox_route_environment["sessionmaker"]
    client = inbox_route_environment["client"]
    created = inbox_route_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"inbox-422-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)
    cookies = {
        "coworker_session": _issue_jwt(user_id=user_id, firm_id=firm_id)
    }

    response = client.get("/api/inbox?top=0", cookies=cookies)
    assert response.status_code == 422
    response = client.get("/api/inbox?top=1001", cookies=cookies)
    assert response.status_code == 422


# --------------------------- auth gate --------------------------------------


def test_inbox_without_cookie_returns_401_generic(
    inbox_route_environment,
) -> None:
    """Confirm the route is gated by current_user.

    Exhaustive auth-failure cases are covered in test_current_user.py.
    """
    client = inbox_route_environment["client"]
    response = client.get("/api/inbox")
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
