"""Integration tests for `refresh_access_token`.

Exercises the helper directly (no TestClient) against the test DB,
with Microsoft's token endpoint mocked via respx.

Each test seeds a firm with Azure credentials and a user with an
encrypted refresh token, then calls refresh_access_token under
`firm_context(firm.id)`. Success-path tests assert the user row was
updated and a graph.token_refreshed audit entry was appended.
Failure-path tests assert the typed exception was raised AND a
graph.token_refresh_failed audit entry with the right `reason` was
committed (even though the caller never commits).
"""
import asyncio
import datetime as _dt
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.auth import refresh_access_token
from coworker.graph.exceptions import ConnectorAuthError, ConnectorTransient
from coworker.security.encryption import decrypt_str, encrypt_str


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def graph_auth_environment(test_database_url):
    """A NullPool engine + sessionmaker against the test DB.

    No monkey-patching — the helper takes `session` as an explicit
    parameter, so we just hand it sessions from this sessionmaker.
    Cleanup tracks created firm ids and drops their rows on teardown.
    """
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sessionmaker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    created_firm_ids: list[uuid.UUID] = []

    try:
        yield {"sessionmaker": sessionmaker, "created_firm_ids": created_firm_ids}
    finally:
        for firm_id in created_firm_ids:
            asyncio.run(_delete_test_firm(sessionmaker, firm_id))
        asyncio.run(engine.dispose())


async def _delete_test_firm(sessionmaker, firm_id: uuid.UUID) -> None:
    """Drop firm/user/audit rows under NO FORCE bracket. ON DELETE RESTRICT
    on audit_log.firm_id requires audit rows be deleted before the firm.
    """
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


def _seed_firm_and_user(
    sessionmaker,
    *,
    slug: str,
    tenant_id: str,
    client_id: str,
    client_secret_plain: str,
    refresh_token_plain: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a firm with Azure creds and a user with a refresh token.

    Both ciphertexts are encrypted under the firm's own AAD so they
    decrypt correctly inside refresh_access_token.
    """
    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Graph Auth Test Firm",
                    slug=slug,
                    azure_tenant_id=tenant_id,
                    azure_client_id=client_id,
                    azure_client_secret_ciphertext=encrypt_str(
                        client_secret_plain, firm_id=firm_id_str
                    ),
                )
            )
            await session.flush()

            user = User(
                firm_id=firm_id,
                azure_object_id=uuid.uuid4().hex,
                upn=f"refresh-test-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Refresh Test User",
                ms_access_token_ciphertext=encrypt_str(
                    "old-access-token", firm_id=firm_id_str
                ),
                ms_refresh_token_ciphertext=encrypt_str(
                    refresh_token_plain, firm_id=firm_id_str
                ),
                ms_token_expires_at=_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(minutes=1),  # already expired
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            await session.commit()
            return firm_id, user_id

    return asyncio.run(_run())


def _load_user_and_firm(sessionmaker, firm_id: uuid.UUID, user_id: uuid.UUID):
    """Load a fresh User + Firm pair under firm_context for a test call."""
    async def _run() -> tuple[Firm, User]:
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            return firm, user

    return asyncio.run(_run())


def _audit_entries_for(sessionmaker, firm_id: uuid.UUID) -> list[AuditLogEntry]:
    """Return all audit entries for a firm, oldest-first."""
    async def _run() -> list[AuditLogEntry]:
        async with sessionmaker() as session, firm_context(firm_id):
            result = await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.firm_id == firm_id)
                .order_by(AuditLogEntry.id.asc())
            )
            return list(result.scalars().all())

    return asyncio.run(_run())


# ------------------------------ happy paths ---------------------------------


def test_refresh_success_updates_columns_and_audits(graph_auth_environment) -> None:
    sessionmaker = graph_auth_environment["sessionmaker"]
    created = graph_auth_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"graph-ok-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-client-secret",
        refresh_token_plain="old-refresh-token",
    )
    created.append(firm_id)

    async def _run() -> None:
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()

            with respx.mock(assert_all_called=True) as rmock:
                rmock.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
                ).mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "access_token": "new-access-token",
                            "refresh_token": "new-refresh-token",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                        },
                    )
                )
                returned = await refresh_access_token(session, user, firm)

            assert returned == "new-access-token"
            await session.commit()

    asyncio.run(_run())

    # Verify persisted state in a fresh session.
    firm, user = _load_user_and_firm(sessionmaker, firm_id, user_id)
    assert (
        decrypt_str(user.ms_access_token_ciphertext, firm_id=str(firm_id))
        == "new-access-token"
    )
    assert (
        decrypt_str(user.ms_refresh_token_ciphertext, firm_id=str(firm_id))
        == "new-refresh-token"
    )
    assert user.ms_token_expires_at is not None
    assert user.ms_token_expires_at > _dt.datetime.now(_dt.timezone.utc)

    audit_entries = _audit_entries_for(sessionmaker, firm_id)
    refresh_entries = [
        e for e in audit_entries if e.action == "graph.token_refreshed"
    ]
    assert len(refresh_entries) == 1
    entry = refresh_entries[0]
    assert entry.payload["user_id"] == str(user_id)
    assert "expires_at" in entry.payload
    assert entry.actor_type == "user"
    assert entry.actor_id == str(user_id)


def test_refresh_without_rotation_keeps_existing_refresh_token(
    graph_auth_environment,
) -> None:
    sessionmaker = graph_auth_environment["sessionmaker"]
    created = graph_auth_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"graph-no-rotate-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-client-secret",
        refresh_token_plain="kept-refresh-token",
    )
    created.append(firm_id)

    async def _run() -> None:
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()

            with respx.mock(assert_all_called=True) as rmock:
                rmock.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
                ).mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "access_token": "new-access-token",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                        },
                    )
                )
                await refresh_access_token(session, user, firm)
            await session.commit()

    asyncio.run(_run())

    firm, user = _load_user_and_firm(sessionmaker, firm_id, user_id)
    # New access token persisted...
    assert (
        decrypt_str(user.ms_access_token_ciphertext, firm_id=str(firm_id))
        == "new-access-token"
    )
    # ...but refresh token is the original.
    assert (
        decrypt_str(user.ms_refresh_token_ciphertext, firm_id=str(firm_id))
        == "kept-refresh-token"
    )


# ------------------------------- failure paths ------------------------------


def test_refresh_4xx_raises_auth_error_and_audits(graph_auth_environment) -> None:
    sessionmaker = graph_auth_environment["sessionmaker"]
    created = graph_auth_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"graph-4xx-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-client-secret",
        refresh_token_plain="revoked-refresh-token",
    )
    created.append(firm_id)

    original_access_ciphertext: bytes | None = None
    original_refresh_ciphertext: bytes | None = None

    async def _run() -> None:
        nonlocal original_access_ciphertext, original_refresh_ciphertext
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            original_access_ciphertext = user.ms_access_token_ciphertext
            original_refresh_ciphertext = user.ms_refresh_token_ciphertext

            with respx.mock(assert_all_called=True) as rmock:
                rmock.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
                ).mock(
                    return_value=httpx.Response(
                        400,
                        json={
                            "error": "invalid_grant",
                            "error_description": "AADSTS70008",
                        },
                    )
                )
                with pytest.raises(ConnectorAuthError):
                    await refresh_access_token(session, user, firm)

    asyncio.run(_run())

    # Failure audit committed by the helper itself.
    audit_entries = _audit_entries_for(sessionmaker, firm_id)
    failed = [e for e in audit_entries if e.action == "graph.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload == {
        "user_id": str(user_id),
        "reason": "microsoft_4xx",
    }
    # And no success audit.
    assert not any(e.action == "graph.token_refreshed" for e in audit_entries)

    # User row tokens unchanged.
    _, user = _load_user_and_firm(sessionmaker, firm_id, user_id)
    assert user.ms_access_token_ciphertext == original_access_ciphertext
    assert user.ms_refresh_token_ciphertext == original_refresh_ciphertext


def test_refresh_5xx_raises_transient_and_audits(graph_auth_environment) -> None:
    sessionmaker = graph_auth_environment["sessionmaker"]
    created = graph_auth_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"graph-5xx-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-client-secret",
        refresh_token_plain="some-refresh-token",
    )
    created.append(firm_id)

    async def _run() -> None:
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()

            with respx.mock(assert_all_called=True) as rmock:
                rmock.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
                ).mock(return_value=httpx.Response(503))
                with pytest.raises(ConnectorTransient):
                    await refresh_access_token(session, user, firm)

    asyncio.run(_run())

    audit_entries = _audit_entries_for(sessionmaker, firm_id)
    failed = [e for e in audit_entries if e.action == "graph.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload == {
        "user_id": str(user_id),
        "reason": "microsoft_5xx",
    }


def test_refresh_network_error_raises_transient_and_audits(
    graph_auth_environment,
) -> None:
    sessionmaker = graph_auth_environment["sessionmaker"]
    created = graph_auth_environment["created_firm_ids"]

    tenant_id = str(uuid.uuid4())
    firm_id, user_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"graph-net-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-client-secret",
        refresh_token_plain="some-refresh-token",
    )
    created.append(firm_id)

    async def _run() -> None:
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()

            with respx.mock(assert_all_called=True) as rmock:
                rmock.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
                ).mock(side_effect=httpx.ConnectError("simulated network failure"))
                with pytest.raises(ConnectorTransient):
                    await refresh_access_token(session, user, firm)

    asyncio.run(_run())

    audit_entries = _audit_entries_for(sessionmaker, firm_id)
    failed = [e for e in audit_entries if e.action == "graph.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload == {
        "user_id": str(user_id),
        "reason": "network_error",
    }


# ------------------------------ invariants ----------------------------------


def test_refresh_user_firm_mismatch_raises_runtime_error(
    graph_auth_environment,
) -> None:
    """user.firm_id != firm.id is a programmer error, never recoverable.

    Use RuntimeError (not ConnectorAuthError) to make clear this is a
    wiring bug, not an authentication failure to be retried or surfaced
    to the user as 'sign in again'.
    """
    sessionmaker = graph_auth_environment["sessionmaker"]
    created = graph_auth_environment["created_firm_ids"]

    firm_a_id, user_a_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"graph-firma-{uuid.uuid4().hex[:8]}",
        tenant_id=str(uuid.uuid4()),
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-a-secret",
        refresh_token_plain="firm-a-refresh",
    )
    created.append(firm_a_id)
    firm_b_id, _user_b_id = _seed_firm_and_user(
        sessionmaker,
        slug=f"graph-firmb-{uuid.uuid4().hex[:8]}",
        tenant_id=str(uuid.uuid4()),
        client_id=str(uuid.uuid4()),
        client_secret_plain="firm-b-secret",
        refresh_token_plain="firm-b-refresh",
    )
    created.append(firm_b_id)

    async def _run() -> None:
        async with sessionmaker() as session, firm_context(firm_a_id):
            user_a = (
                await session.execute(select(User).where(User.id == user_a_id))
            ).scalar_one()
        async with sessionmaker() as session, firm_context(firm_b_id):
            firm_b = (
                await session.execute(select(Firm).where(Firm.id == firm_b_id))
            ).scalar_one()

        # Now call with mismatched (user_a, firm_b). No HTTP mocking
        # because the helper must reject before any network call.
        async with sessionmaker() as session, firm_context(firm_b_id):
            with pytest.raises(RuntimeError, match="programmer error"):
                await refresh_access_token(session, user_a, firm_b)

    asyncio.run(_run())
