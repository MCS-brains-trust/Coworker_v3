"""End-to-end tests for the approval queue HTTP routes.

FastAPI TestClient + a real DB. Each test seeds firm, principal,
optional pre-existing items, issues a session JWT cookie, and
hits the routes.
"""
import datetime as _dt
import uuid

import jwt
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.api.main import app
from coworker.approval.items import CreateApprovalInput, create_approval
from coworker.config import get_settings
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest_asyncio.fixture
async def routes_env(test_database_url, monkeypatch):
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
    slug = f"appr-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        firm = Firm(id=firm_id, name="Approval Firm", slug=slug)
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Principal",
        )
        session.add_all([firm, user])
        await session.commit()
        user_id = user.id

    try:
        yield {
            "sm": sm,
            "firm_id": firm_id,
            "user_id": user_id,
            "slug": slug,
        }
    finally:
        await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = ("firms", "users", "audit_log", "approval_items")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in ("approval_items", "audit_log", "users"):
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


async def _seed_item(
    sm, firm_id: uuid.UUID, *, summary: str = "Draft",
) -> uuid.UUID:
    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary=summary,
                payload={
                    "to": ["c@example.com"],
                    "subject": "Re: foo",
                    "body_html": "<p>hi</p>",
                },
            ),
        )
        await session.commit()
        return row.id


def _client(*, user_id: uuid.UUID, firm_id: uuid.UUID) -> TestClient:
    client = TestClient(app)
    client.cookies.set("coworker_session", _issue_jwt(
        user_id=user_id, firm_id=firm_id,
    ))
    return client


# ===========================================================================
# Tests
# ===========================================================================


def test_list_pending_returns_seeded_items(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    a = asyncio.run(_seed_item(sm, firm_id, summary="First"))
    b = asyncio.run(_seed_item(sm, firm_id, summary="Second"))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/approval/pending")
    assert resp.status_code == 200
    body = resp.json()
    ids = {item["id"] for item in body}
    assert ids == {str(a), str(b)}
    summaries = {item["summary"] for item in body}
    assert summaries == {"First", "Second"}


def test_pending_requires_auth(routes_env) -> None:
    client = TestClient(app)  # no cookie
    resp = client.get("/approval/pending")
    assert resp.status_code == 401


def test_get_one_returns_404_for_missing(routes_env) -> None:
    user_id = routes_env["user_id"]
    firm_id = routes_env["firm_id"]
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get(f"/approval/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_one_returns_item(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_id, summary="One"))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get(f"/approval/{item_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(item_id)
    assert body["status"] == "pending"


def test_approve_transitions_pending_to_approved(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_id))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.post(
        f"/approval/{item_id}/approve",
        json={"notes": "LGTM"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decision_notes"] == "LGTM"
    assert body["decided_by_user_id"] == str(user_id)

    # Persisted.
    async def _check() -> str:
        async with sm() as session, firm_context(firm_id):
            row = (
                await session.execute(
                    select(ApprovalItem).where(ApprovalItem.id == item_id)
                )
            ).scalar_one()
            return row.status
    assert asyncio.run(_check()) == "approved"


def test_approve_already_decided_returns_409(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_id))

    client = _client(user_id=user_id, firm_id=firm_id)
    first = client.post(f"/approval/{item_id}/approve")
    assert first.status_code == 200

    second = client.post(f"/approval/{item_id}/approve")
    assert second.status_code == 409


def test_reject_transitions_pending_to_rejected(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_id))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.post(
        f"/approval/{item_id}/reject",
        json={"notes": "Wrong tone"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["decision_notes"] == "Wrong tone"


def test_approve_unknown_id_returns_404(routes_env) -> None:
    user_id = routes_env["user_id"]
    firm_id = routes_env["firm_id"]
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.post(f"/approval/{uuid.uuid4()}/approve")
    assert resp.status_code == 404


def test_cross_firm_get_returns_404(routes_env) -> None:
    """A user from firm B cannot read firm A's item — RLS hides it."""
    sm = routes_env["sm"]
    firm_a_id = routes_env["firm_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_a_id))

    # Seed an unrelated firm + user.
    firm_b_id = uuid.uuid4()
    other_user_id = uuid.uuid4()

    async def _seed_other() -> None:
        async with sm() as session, firm_context(firm_b_id):
            session.add(Firm(
                id=firm_b_id, name="Other Firm",
                slug=f"o-{uuid.uuid4().hex[:8]}",
            ))
            session.add(User(
                id=other_user_id, firm_id=firm_b_id,
                azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
                upn=f"o-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Other",
            ))
            await session.commit()
    asyncio.run(_seed_other())

    try:
        client = _client(user_id=other_user_id, firm_id=firm_b_id)
        resp = client.get(f"/approval/{item_id}")
        assert resp.status_code == 404
        # Approve also 404.
        resp2 = client.post(f"/approval/{item_id}/approve")
        assert resp2.status_code == 404
    finally:
        asyncio.run(_cleanup_firm(sm, firm_b_id))


def test_limit_validation(routes_env) -> None:
    user_id = routes_env["user_id"]
    firm_id = routes_env["firm_id"]
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/approval/pending?limit=0")
    assert resp.status_code == 400
    resp = client.get("/approval/pending?limit=10000")
    assert resp.status_code == 400


def test_notes_too_long_rejected(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_id))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.post(
        f"/approval/{item_id}/approve",
        json={"notes": "x" * 2001},
    )
    assert resp.status_code == 422
    # Silence linter on pytest import if unused elsewhere
    assert pytest is not None


# ===========================================================================
# Phase 9-3: in-place edit route
# ===========================================================================


def test_edit_payload_route_updates_pending(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_id))

    client = _client(user_id=user_id, firm_id=firm_id)
    new_payload = {
        "to": ["client@example.com"],
        "subject": "Re: your query (edited)",
        "body_html": "<p>Better wording.</p>",
    }
    resp = client.put(
        f"/approval/{item_id}/payload",
        json={"payload": new_payload},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["payload"]["body_html"] == "<p>Better wording.</p>"
    assert body["last_edited_by_user_id"] == str(user_id)
    assert body["last_edited_at"] is not None


def test_edit_payload_after_approve_returns_409(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    import asyncio
    item_id = asyncio.run(_seed_item(sm, firm_id))

    client = _client(user_id=user_id, firm_id=firm_id)
    approved = client.post(f"/approval/{item_id}/approve")
    assert approved.status_code == 200

    resp = client.put(
        f"/approval/{item_id}/payload",
        json={"payload": {"different": "shape"}},
    )
    assert resp.status_code == 409


def test_edit_payload_missing_returns_404(routes_env) -> None:
    user_id = routes_env["user_id"]
    firm_id = routes_env["firm_id"]
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/approval/{uuid.uuid4()}/payload",
        json={"payload": {}},
    )
    assert resp.status_code == 404
