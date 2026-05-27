"""End-to-end tests for /api/v1/specialists.

Uses the same FastAPI TestClient + real-DB pattern as
``test_approval_routes.py``. Each test seeds a firm + a user (with
configurable role), optionally seeds specialists, and hits the routes.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import uuid

import jwt
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
from coworker.config import get_settings
from coworker.db.models import (
    AuditLogEntry,
    Firm,
    Specialist,
    SpecialistPromptVersion,
    User,
)
from coworker.db.session import _attach_pool_listeners, firm_context

_CLEANUP_TABLES = (
    "firms",
    "users",
    "audit_log",
    "specialists",
    "specialist_prompt_versions",
)


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
    slug = f"spec-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Specialist Firm", slug=slug))
        await session.commit()

    try:
        yield {"sm": sm, "firm_id": firm_id, "slug": slug}
    finally:
        await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id: uuid.UUID) -> None:
    async with sm() as session:
        for t in _CLEANUP_TABLES:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            # Order matters: drop the back-pointer first, then versions,
            # then specialists, then existing dependents.
            await session.execute(
                text(
                    "UPDATE specialists SET active_version_id = NULL "
                    "WHERE firm_id = :id"
                ),
                {"id": str(firm_id)},
            )
            for t in (
                "specialist_prompt_versions",
                "specialists",
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
        for t in _CLEANUP_TABLES:
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


async def _seed_user(
    sm, firm_id: uuid.UUID, *, role: str = "accountant"
) -> uuid.UUID:
    async with sm() as session, firm_context(firm_id):
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name=f"User {role}",
            role=role,
        )
        session.add(user)
        await session.commit()
        return user.id


async def _seed_specialist(
    sm,
    firm_id: uuid.UUID,
    *,
    name: str,
    display_name: str,
    prompt_text: str = "x" * 500,
) -> uuid.UUID:
    async with sm() as session, firm_context(firm_id):
        spec = Specialist(
            firm_id=firm_id,
            name=name,
            display_name=display_name,
            description=f"Description for {name}",
            model="claude-opus-4-7",
            extended_thinking=True,
        )
        session.add(spec)
        await session.flush()
        version = SpecialistPromptVersion(
            firm_id=firm_id,
            specialist_id=spec.id,
            version_number=1,
            prompt_text=prompt_text,
            status="active",
            change_summary="seed",
        )
        session.add(version)
        await session.flush()
        spec.active_version_id = version.id
        await session.commit()
        return spec.id


# ===========================================================================
# GET /api/v1/specialists
# ===========================================================================


def test_list_specialists_empty_firm(routes_env) -> None:
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(routes_env["sm"], firm_id))
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/specialists")
    assert resp.status_code == 200
    assert resp.json() == {"specialists": []}


def test_list_specialists_after_seed(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    asyncio.run(_seed_specialist(sm, firm_id, name="gst", display_name="GST"))
    asyncio.run(
        _seed_specialist(sm, firm_id, name="smsf", display_name="SMSF")
    )
    asyncio.run(
        _seed_specialist(sm, firm_id, name="div7a", display_name="Div 7A")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/specialists")
    assert resp.status_code == 200
    body = resp.json()
    names = [s["display_name"] for s in body["specialists"]]
    assert names == sorted(names)
    assert {s["name"] for s in body["specialists"]} == {
        "gst", "smsf", "div7a"
    }


# ===========================================================================
# GET /api/v1/specialists/{id}/prompt
# ===========================================================================


def test_get_specialist_prompt_returns_body(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    spec_id = asyncio.run(
        _seed_specialist(
            sm, firm_id, name="gst", display_name="GST",
            prompt_text="You are the GST specialist." + " " * 200,
        )
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get(f"/api/v1/specialists/{spec_id}/prompt")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "gst"
    assert body["version_number"] == 1
    assert body["prompt_text"].startswith("You are the GST specialist.")


def test_get_specialist_prompt_404_when_unknown_id(routes_env) -> None:
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(routes_env["sm"], firm_id))
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get(f"/api/v1/specialists/{uuid.uuid4()}/prompt")
    assert resp.status_code == 404


def test_get_specialist_prompt_404_when_no_active_version(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id))
    # Manually seed a row with active_version_id=NULL (no version row).
    async def _seed_dangling() -> uuid.UUID:
        async with sm() as session, firm_context(firm_id):
            spec = Specialist(
                firm_id=firm_id,
                name="dangling",
                display_name="Dangling",
                description="no active version",
                model="claude-opus-4-7",
                extended_thinking=True,
            )
            session.add(spec)
            await session.commit()
            return spec.id
    spec_id = asyncio.run(_seed_dangling())

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get(f"/api/v1/specialists/{spec_id}/prompt")
    assert resp.status_code == 404


# ===========================================================================
# PUT /api/v1/specialists/{id}/prompt
# ===========================================================================


def test_put_specialist_prompt_updates_text(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="principal"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    new_text = "Updated prompt body. " * 30
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": new_text,
            "change_summary": "tightened wording",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version_number"] == 2
    assert body["prompt_text"] == new_text

    # Persisted: GET now returns the new text + v2.
    follow = client.get(f"/api/v1/specialists/{spec_id}/prompt")
    assert follow.status_code == 200
    assert follow.json()["version_number"] == 2
    assert follow.json()["prompt_text"] == new_text


def test_put_specialist_prompt_retires_previous_version(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="owner"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": "x" * 200,
            "change_summary": "test retire",
        },
    )
    assert resp.status_code == 200

    async def _check() -> tuple[int, int]:
        async with sm() as session, firm_context(firm_id):
            rows = (
                await session.execute(
                    select(SpecialistPromptVersion)
                    .where(SpecialistPromptVersion.specialist_id == spec_id)
                    .order_by(SpecialistPromptVersion.version_number)
                )
            ).scalars().all()
            active = sum(1 for r in rows if r.status == "active")
            retired = sum(1 for r in rows if r.status == "retired")
            return active, retired
    active, retired = asyncio.run(_check())
    assert active == 1
    assert retired == 1


def test_put_specialist_prompt_role_accountant_403(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="accountant"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": "x" * 200,
            "change_summary": "no permission",
        },
    )
    assert resp.status_code == 403


def test_put_specialist_prompt_role_viewer_403(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="viewer"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": "x" * 200,
            "change_summary": "no permission",
        },
    )
    assert resp.status_code == 403


def test_put_specialist_prompt_role_owner_200(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="owner"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": "x" * 200,
            "change_summary": "owner edit",
        },
    )
    assert resp.status_code == 200


def test_put_specialist_prompt_role_principal_200(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="principal"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": "x" * 200,
            "change_summary": "principal edit",
        },
    )
    assert resp.status_code == 200


def test_put_specialist_prompt_writes_audit_log(routes_env) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="principal"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": "x" * 200,
            "change_summary": "audit me please",
        },
    )
    assert resp.status_code == 200

    async def _check() -> AuditLogEntry:
        async with sm() as session, firm_context(firm_id):
            row = (
                await session.execute(
                    select(AuditLogEntry).where(
                        AuditLogEntry.action == "specialist.prompt_updated"
                    )
                )
            ).scalar_one()
            return row
    entry = asyncio.run(_check())
    assert entry.target_type == "specialist"
    assert entry.target_id == str(spec_id)
    assert entry.payload["change_summary"] == "audit me please"
    assert entry.payload["prev_version"] == 1
    assert entry.payload["new_version"] == 2


def test_put_specialist_prompt_minimum_change_summary_length(
    routes_env,
) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = asyncio.run(_seed_user(sm, firm_id, role="principal"))
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.put(
        f"/api/v1/specialists/{spec_id}/prompt",
        json={
            "prompt_text": "x" * 200,
            "change_summary": "tiny",
        },
    )
    assert resp.status_code == 422


# ===========================================================================
# Cross-firm isolation
# ===========================================================================


def test_cross_firm_isolation(routes_env) -> None:
    """A user from firm B cannot read firm A's specialist — RLS hides it."""
    sm = routes_env["sm"]
    firm_a_id = routes_env["firm_id"]
    spec_id = asyncio.run(
        _seed_specialist(sm, firm_a_id, name="gst", display_name="GST")
    )

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
                role="owner",
            ))
            await session.commit()

    asyncio.run(_seed_other())

    try:
        client = _client(user_id=other_user_id, firm_id=firm_b_id)
        resp = client.get(f"/api/v1/specialists/{spec_id}/prompt")
        assert resp.status_code == 404
        # PUT also 404 (not 403, because RLS hides the row before the
        # role check matters — but the role check is structural, not
        # RLS, so a PUT from firm B as owner should still 404 at the
        # row lookup).
        put_resp = client.put(
            f"/api/v1/specialists/{spec_id}/prompt",
            json={
                "prompt_text": "x" * 200,
                "change_summary": "cross firm",
            },
        )
        assert put_resp.status_code == 404
    finally:
        asyncio.run(_cleanup_firm(sm, firm_b_id))
