"""Integration tests for the `current_user` FastAPI dependency.

End-to-end through TestClient: real DB (test instance), real session JWT
signed with the configured SESSION_JWT_SECRET. We mount `current_user`
on a stub `/whoami` route so the dependency is exercised exactly as a
real route would exercise it.

DB redirection follows the same pattern as `test_auth_routes.py`:
monkey-patch `get_sessionmaker` and `get_engine` so `Depends(get_session)`
inside the dependency resolves against the test database. We seed a
firm + user with real commits (via the patched sessionmaker, not the
savepoint-rollback `db_session` fixture) because the request runs in
TestClient's own event loop and needs the rows to be visible across
loops.

Every failure case asserts BOTH the 401 status and the identical
response body. That's the load-bearing test: if we ever accidentally
distinguish "no cookie" from "expired" from "user gone", an attacker
can map valid users by probing.
"""
import asyncio
import datetime as _dt
import uuid

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from coworker.api.deps import current_user
from coworker.config import get_settings
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context


_GENERIC_BODY = {"detail": "authentication required"}


@pytest.fixture
def deps_test_environment(test_database_url, monkeypatch):
    """Mini FastAPI app with /whoami protected by `current_user`.

    Mirrors test_auth_routes.auth_test_environment: a NullPool engine
    on the test DB, sessionmaker patched into `coworker.db.session`, a
    stub route that returns the resolved user's id and firm_id, and a
    teardown that drops the firm/user/audit rows we created.
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

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: User = Depends(current_user)) -> dict[str, str]:
        return {"user_id": str(user.id), "firm_id": str(user.firm_id), "upn": user.upn}

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
    """Lift FORCE on tenant tables, delete this firm's rows, restore FORCE.

    Matches the cleanup helper in test_auth_routes.py. audit_log is
    deleted first because audit_log.firm_id has ON DELETE RESTRICT.
    """
    _TABLES = ("firms", "users", "audit_log")
    async with sessionmaker() as session:
        for t in _TABLES:
            await session.execute(text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
        try:
            await session.execute(
                text("DELETE FROM audit_log WHERE firm_id = :id"), {"id": str(firm_id)}
            )
            await session.execute(
                text("DELETE FROM users WHERE firm_id = :id"), {"id": str(firm_id)}
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


def _seed_firm_and_user(
    sessionmaker, *, slug: str, upn: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a firm and one user inside it. Returns (firm_id, user_id).

    Pre-allocates the firm UUID so we can enter `firm_context(firm_id)`
    BEFORE the firm INSERT — the firms-table RLS policy's INSERT WITH
    CHECK is `id = app.firm_id`, which would fail if we let SQLAlchemy
    default the UUID after the GUC was already set.
    """
    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="current_user Test Firm",
                    slug=slug,
                    azure_tenant_id=str(uuid.uuid4()),
                    azure_client_id=str(uuid.uuid4()),
                )
            )
            await session.flush()
            user = User(
                firm_id=firm_id,
                azure_object_id=uuid.uuid4().hex,
                upn=upn,
                display_name="Current User Test",
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            await session.commit()
            return firm_id, user_id
    return asyncio.run(_run())


def _issue_jwt(
    *,
    user_id: uuid.UUID,
    firm_id: uuid.UUID,
    ttl_seconds: int = 60,
    secret: str | None = None,
    algorithm: str = "HS256",
) -> str:
    """Build a JWT with explicit control over secret/TTL for adversarial tests."""
    now = _dt.datetime.now(_dt.timezone.utc)
    claims = {
        "sub": str(user_id),
        "firm_id": str(firm_id),
        "iat": int(now.timestamp()),
        "exp": int((now + _dt.timedelta(seconds=ttl_seconds)).timestamp()),
    }
    if secret is None:
        secret = get_settings().SESSION_JWT_SECRET.get_secret_value()
    return jwt.encode(claims, secret, algorithm=algorithm)


# ----------------------------- happy path ----------------------------------


def test_valid_jwt_returns_user(deps_test_environment) -> None:
    sessionmaker = deps_test_environment["sessionmaker"]
    client = deps_test_environment["client"]
    created = deps_test_environment["created_firm_ids"]

    slug = f"deps-ok-{uuid.uuid4().hex[:8]}"
    upn = f"alice-{uuid.uuid4().hex[:8]}@example.com"
    firm_id, user_id = _seed_firm_and_user(sessionmaker, slug=slug, upn=upn)
    created.append(firm_id)

    token = _issue_jwt(user_id=user_id, firm_id=firm_id)
    response = client.get("/whoami", cookies={"coworker_session": token})

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "user_id": str(user_id),
        "firm_id": str(firm_id),
        "upn": upn,
    }


# --------------------------- failure paths ---------------------------------


def test_missing_cookie_returns_401_generic(deps_test_environment) -> None:
    response = deps_test_environment["client"].get("/whoami")
    assert response.status_code == 401
    assert response.json() == _GENERIC_BODY


def test_expired_jwt_returns_401_generic(deps_test_environment) -> None:
    sessionmaker = deps_test_environment["sessionmaker"]
    client = deps_test_environment["client"]
    created = deps_test_environment["created_firm_ids"]

    slug = f"deps-expired-{uuid.uuid4().hex[:8]}"
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker, slug=slug, upn=f"bob-{uuid.uuid4().hex[:8]}@example.com"
    )
    created.append(firm_id)

    expired_token = _issue_jwt(user_id=user_id, firm_id=firm_id, ttl_seconds=-60)
    response = client.get("/whoami", cookies={"coworker_session": expired_token})

    assert response.status_code == 401
    assert response.json() == _GENERIC_BODY


def test_bad_signature_jwt_returns_401_generic(deps_test_environment) -> None:
    sessionmaker = deps_test_environment["sessionmaker"]
    client = deps_test_environment["client"]
    created = deps_test_environment["created_firm_ids"]

    slug = f"deps-badsig-{uuid.uuid4().hex[:8]}"
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker, slug=slug, upn=f"carol-{uuid.uuid4().hex[:8]}@example.com"
    )
    created.append(firm_id)

    forged_token = _issue_jwt(
        user_id=user_id, firm_id=firm_id, secret="not-the-real-secret"
    )
    response = client.get("/whoami", cookies={"coworker_session": forged_token})

    assert response.status_code == 401
    assert response.json() == _GENERIC_BODY


def test_jwt_for_missing_user_returns_401_generic(deps_test_environment) -> None:
    """Well-formed JWT, valid signature, but `sub` doesn't match any row.

    Could happen after a user is deleted (firm offboarding) while their
    cookie is still in flight; could also be a forged-claim attempt
    that pairs a real firm_id with a synthesised user_id.
    """
    sessionmaker = deps_test_environment["sessionmaker"]
    client = deps_test_environment["client"]
    created = deps_test_environment["created_firm_ids"]

    slug = f"deps-nouser-{uuid.uuid4().hex[:8]}"
    firm_id, _real_user_id = _seed_firm_and_user(
        sessionmaker, slug=slug, upn=f"dave-{uuid.uuid4().hex[:8]}@example.com"
    )
    created.append(firm_id)

    token = _issue_jwt(user_id=uuid.uuid4(), firm_id=firm_id)
    response = client.get("/whoami", cookies={"coworker_session": token})

    assert response.status_code == 401
    assert response.json() == _GENERIC_BODY


def test_cross_firm_jwt_returns_401_generic(deps_test_environment) -> None:
    """JWT whose `sub` is a real user but `firm_id` claim is a different firm.

    RLS scopes the SELECT to firm_id_claim; the user belongs to a
    different firm, so the SELECT returns zero rows. This is the
    defence-in-depth property: even a forged JWT (which would require
    leaking SESSION_JWT_SECRET) cannot let an attacker pivot into a
    user in a firm whose identity their forgery doesn't already imply.
    """
    sessionmaker = deps_test_environment["sessionmaker"]
    client = deps_test_environment["client"]
    created = deps_test_environment["created_firm_ids"]

    firm_a_id, user_a_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"deps-firma-{uuid.uuid4().hex[:8]}",
        upn=f"eve-{uuid.uuid4().hex[:8]}@example.com",
    )
    created.append(firm_a_id)
    firm_b_id, _user_b_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"deps-firmb-{uuid.uuid4().hex[:8]}",
        upn=f"frank-{uuid.uuid4().hex[:8]}@example.com",
    )
    created.append(firm_b_id)

    token = _issue_jwt(user_id=user_a_id, firm_id=firm_b_id)
    response = client.get("/whoami", cookies={"coworker_session": token})

    assert response.status_code == 401
    assert response.json() == _GENERIC_BODY


def test_all_failure_paths_return_identical_body(deps_test_environment) -> None:
    """All five 401 cases must return byte-identical bodies.

    Captures the no-info-leak invariant directly: if any future change
    diverges the response (a header, a hint, a different detail), this
    test fails before it ships.
    """
    sessionmaker = deps_test_environment["sessionmaker"]
    client = deps_test_environment["client"]
    created = deps_test_environment["created_firm_ids"]

    slug = f"deps-identical-{uuid.uuid4().hex[:8]}"
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker, slug=slug, upn=f"grace-{uuid.uuid4().hex[:8]}@example.com"
    )
    created.append(firm_id)

    bodies: list[str] = []

    # 1. No cookie
    bodies.append(client.get("/whoami").text)

    # 2. Garbage cookie (decode fails)
    bodies.append(
        client.get("/whoami", cookies={"coworker_session": "not-a-jwt"}).text
    )

    # 3. Expired JWT
    bodies.append(
        client.get(
            "/whoami",
            cookies={
                "coworker_session": _issue_jwt(
                    user_id=user_id, firm_id=firm_id, ttl_seconds=-60
                )
            },
        ).text
    )

    # 4. Bad signature
    bodies.append(
        client.get(
            "/whoami",
            cookies={
                "coworker_session": _issue_jwt(
                    user_id=user_id, firm_id=firm_id, secret="wrong-secret"
                )
            },
        ).text
    )

    # 5. Valid JWT for a user that doesn't exist
    bodies.append(
        client.get(
            "/whoami",
            cookies={
                "coworker_session": _issue_jwt(
                    user_id=uuid.uuid4(), firm_id=firm_id
                )
            },
        ).text
    )

    assert len(set(bodies)) == 1, f"failure-path bodies diverge: {set(bodies)}"
