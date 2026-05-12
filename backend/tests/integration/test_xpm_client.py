"""Integration tests for ``coworker.connectors.xpm_client.XPMClient``.

Pattern matches ``test_graph_auth.py``: real DB, mocked HTTP via
respx. Tests in this file cover Phase 3E-2 (OAuth refresh scaffolding
only); read/write XPM methods land in later sub-phases with their
own tests.
"""
import asyncio
import datetime as _dt
import uuid
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorNotFound,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.connectors.xpm_client import (
    XPMClient,
    XPMClientRecord,
    XPMInvoice,
    XPMJob,
    XPMRelationship,
)
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import decrypt_str, encrypt_str

_TOKEN_URL = "https://identity.xero.com/connect/token"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def xpm_environment(test_database_url):
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
                text("DELETE FROM firms WHERE id = :id"), {"id": str(firm_id)}
            )
            await session.commit()
        finally:
            for t in tables:
                await session.execute(
                    text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
                )
            await session.commit()


def _seed_firm(
    sessionmaker,
    *,
    slug: str,
    xpm_client_id: str | None = "xpm-client-id",
    xpm_client_secret: str | None = "xpm-secret-123",
    xpm_refresh_token: str | None = "refresh-tok-1",
    xpm_access_token: str | None = "access-tok-1",
    expires_at: _dt.datetime | None = None,
    xpm_account_id: str | None = "tenant-aaa",
) -> uuid.UUID:
    """Seed a firm with XPM credentials. Returns firm_id."""

    async def _run() -> uuid.UUID:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            kwargs: dict = {
                "id": firm_id,
                "name": "XPM Test Firm",
                "slug": slug,
                "xpm_client_id": xpm_client_id,
                "xpm_account_id": xpm_account_id,
            }
            if xpm_client_secret is not None:
                kwargs["xpm_client_secret_ciphertext"] = encrypt_str(
                    xpm_client_secret, firm_id=firm_id_str
                )
            if xpm_refresh_token is not None:
                kwargs["xpm_refresh_token_ciphertext"] = encrypt_str(
                    xpm_refresh_token, firm_id=firm_id_str
                )
            if xpm_access_token is not None:
                kwargs["xpm_access_token_ciphertext"] = encrypt_str(
                    xpm_access_token, firm_id=firm_id_str
                )
            if expires_at is not None:
                kwargs["xpm_token_expires_at"] = expires_at
            session.add(Firm(**kwargs))
            await session.commit()
            return firm_id

    return asyncio.run(_run())


def _audit_entries(sessionmaker, firm_id: uuid.UUID) -> list[AuditLogEntry]:
    async def _run() -> list[AuditLogEntry]:
        async with sessionmaker() as session, firm_context(firm_id):
            result = await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.firm_id == firm_id)
                .order_by(AuditLogEntry.id.asc())
            )
            return list(result.scalars().all())

    return asyncio.run(_run())


def _load_firm(sessionmaker, firm_id: uuid.UUID) -> Firm:
    async def _run() -> Firm:
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            return firm

    return asyncio.run(_run())


def _run_with_firm(sessionmaker, firm_id, body):
    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            return await body(session, firm)

    return asyncio.run(_run())


def _token_response(
    *,
    access_token: str = "new-access",
    refresh_token: str = "new-refresh",
    expires_in: int = 1800,
) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "token_type": "Bearer",
        "scope": "practicemanager offline_access",
    }


# =========================================================================
# _refresh_access_token
# =========================================================================


def test_refresh_persists_rotated_tokens_and_audits(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=_token_response(
                        access_token="rotated-access",
                        refresh_token="rotated-refresh",
                        expires_in=1800,
                    ),
                )
            )
            access = await client._refresh_access_token()
        # Verify the wire request.
        sent = route.calls.last.request
        body_str = sent.read().decode()
        assert "grant_type=refresh_token" in body_str
        assert "refresh_token=refresh-tok-1" in body_str
        # Basic auth with client_id:client_secret
        assert sent.headers["Authorization"].startswith("Basic ")
        return access

    new_access = _run_with_firm(sm, firm_id, body)
    assert new_access == "rotated-access"

    # The firm row was mutated and committed. Re-read.
    firm = _load_firm(sm, firm_id)
    assert firm.xpm_access_token_ciphertext is not None
    assert firm.xpm_refresh_token_ciphertext is not None
    firm_id_str = str(firm_id)
    assert decrypt_str(
        firm.xpm_access_token_ciphertext, firm_id=firm_id_str
    ) == "rotated-access"
    assert decrypt_str(
        firm.xpm_refresh_token_ciphertext, firm_id=firm_id_str
    ) == "rotated-refresh"
    assert firm.xpm_token_expires_at is not None
    assert firm.xpm_token_expires_at > _dt.datetime.now(_dt.UTC) + _dt.timedelta(
        minutes=20
    )

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.token_refreshed"]
    assert len(success) == 1
    assert success[0].actor_type == "system"
    assert success[0].actor_id == "system"
    assert success[0].payload["expires_in"] == 1800


def test_refresh_400_invalid_grant_raises_auth_error_and_audits(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-400-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"})
            )
            with pytest.raises(ConnectorAuthError):
                await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_400"
    assert failed[0].actor_type == "system"


def test_refresh_5xx_raises_transient_and_audits(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(ConnectorTransient):
                await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_5xx"


def test_refresh_network_error_raises_transient_and_audits(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(side_effect=httpx.ConnectError("no net"))
            with pytest.raises(ConnectorTransient):
                await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


def test_refresh_missing_refresh_token_raises_auth_error(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-norefresh-{uuid.uuid4().hex[:8]}",
        xpm_refresh_token=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ConnectorAuthError):
            await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "missing_refresh_token"


def test_refresh_missing_client_credentials_raises_auth_error(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-nocreds-{uuid.uuid4().hex[:8]}",
        xpm_client_id=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ConnectorAuthError):
            await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "missing_client_credentials"


def test_refresh_with_user_actor_records_user_actor_on_audit(
    xpm_environment,
) -> None:
    """A UI-initiated refresh records actor_type='user' and actor_id=user_id."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-user-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    user_id = "fake-user-uuid-string"

    async def body(session, firm):
        client = XPMClient(
            firm, session=session, actor_id=user_id, actor_type="user"
        )
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.token_refreshed"]
    assert len(success) == 1
    assert success[0].actor_type == "user"
    assert success[0].actor_id == user_id


# =========================================================================
# _ensure_access_token (cache vs refresh decision)
# =========================================================================


def test_ensure_access_token_uses_cached_token_when_valid(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    # Seeded with an access token valid for another hour — should not refresh.
    firm_id = _seed_firm(
        sm,
        slug=f"xpm-cached-{uuid.uuid4().hex[:8]}",
        xpm_access_token="cached-access",
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        # respx with no mocks: any HTTP attempt raises.
        with respx.mock():
            token = await client._ensure_access_token()
        return token

    token = _run_with_firm(sm, firm_id, body)
    assert token == "cached-access"

    # No audit rows: no refresh happened.
    audits = _audit_entries(sm, firm_id)
    assert not any(a.action == "xpm.token_refreshed" for a in audits)


def test_ensure_access_token_refreshes_when_within_buffer(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    # 30 seconds from expiry — well inside the 5-min refresh buffer.
    firm_id = _seed_firm(
        sm,
        slug=f"xpm-soonexpire-{uuid.uuid4().hex[:8]}",
        xpm_access_token="stale-access",
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=30),
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=_token_response(access_token="freshly-refreshed"),
                )
            )
            return await client._ensure_access_token()

    token = _run_with_firm(sm, firm_id, body)
    assert token == "freshly-refreshed"


def test_ensure_access_token_refreshes_when_expiry_missing(xpm_environment) -> None:
    """Firm with no recorded expiry triggers a refresh — incomplete onboarding."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-noexp-{uuid.uuid4().hex[:8]}",
        xpm_access_token="any",
        expires_at=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            return await client._ensure_access_token()

    _run_with_firm(sm, firm_id, body)


def test_ensure_access_token_refreshes_when_access_ciphertext_missing(
    xpm_environment,
) -> None:
    """Firm has expires_at but no access ciphertext — inconsistent row, refresh."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-noaccess-{uuid.uuid4().hex[:8]}",
        xpm_access_token=None,  # but expires_at still set far in future
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            return await client._ensure_access_token()

    _run_with_firm(sm, firm_id, body)


# =========================================================================
# list_clients
# =========================================================================


_CLIENTS_LIST_URL = (
    "https://api.xero.com/practicemanager/3.1/clients.api/list"
)
_CLIENT_GET_URL_PREFIX = (
    "https://api.xero.com/practicemanager/3.1/client.api/get"
)


def _client_payload(
    *,
    cid: str = "c-1",
    name: str = "Acme Pty Ltd",
    email: str = "info@acme.example",
    phone: str = "0412 000 000",
    is_active: bool = True,
    entity_type: str = "Company",
    created: str = "2024-01-15T10:00:00",
    modified: str = "2024-05-01T11:00:00",
) -> dict:
    return {
        "ID": cid,
        "Name": name,
        "Email": email,
        "Phone": phone,
        "IsActive": is_active,
        "Type": entity_type,
        "CreatedDate": created,
        "ModifiedDate": modified,
    }


def _seed_with_valid_token(sm, *, slug: str) -> uuid.UUID:
    """Seed a firm whose access token is valid for another hour."""
    return _seed_firm(
        sm,
        slug=slug,
        xpm_access_token="cached-access",
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
    )


def test_list_clients_returns_parsed_records_and_audits(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lc-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "Clients": [
            _client_payload(cid="c-1", name="Acme Pty Ltd"),
            _client_payload(
                cid="c-2", name="Beta Trust", entity_type="Trust"
            ),
        ]
    }

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_CLIENTS_LIST_URL).mock(
                return_value=httpx.Response(200, json=payload)
            )
            clients = await client.list_clients()
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer cached-access"
        assert sent.headers["Xero-Tenant-Id"] == "tenant-aaa"
        assert sent.headers["Accept"] == "application/json"
        assert "modifiedsince" not in sent.url.params
        return clients

    clients = _run_with_firm(sm, firm_id, body)
    assert len(clients) == 2
    assert all(isinstance(c, XPMClientRecord) for c in clients)
    assert clients[0].id == "c-1"
    assert clients[0].name == "Acme Pty Ltd"
    assert clients[0].entity_type == "Company"
    assert clients[0].created_at.tzinfo is not None
    assert clients[1].entity_type == "Trust"

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.clients.list"]
    assert len(success) == 1
    assert success[0].payload["count"] == 2


def test_list_clients_passes_modifiedsince_for_incremental_sync(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lcms-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    updated_since = _dt.datetime(2025, 4, 1, 12, 0, 0, tzinfo=_dt.UTC)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_CLIENTS_LIST_URL).mock(
                return_value=httpx.Response(200, json={"Clients": []})
            )
            await client.list_clients(updated_since=updated_since)
        sent = route.calls.last.request
        assert sent.url.params["modifiedsince"] == "2025-04-01T12:00:00"

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.clients.list"]
    assert success[0].payload["modifiedsince"] == "2025-04-01T12:00:00"


def test_list_clients_handles_response_envelope(xpm_environment) -> None:
    """Tolerates {"Response": {"Clients": [...]}} wrapper too."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-env-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_CLIENTS_LIST_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"Response": {"Clients": [_client_payload(cid="x")]}},
                )
            )
            return await client.list_clients()

    clients = _run_with_firm(sm, firm_id, body)
    assert len(clients) == 1
    assert clients[0].id == "x"


def test_list_clients_401_raises_auth_error_and_audits(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lc401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_CLIENTS_LIST_URL).mock(
                return_value=httpx.Response(401)
            )
            with pytest.raises(ConnectorAuthError):
                await client.list_clients()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.clients.list_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_401"


def test_list_clients_429_with_retry_after(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-429-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_CLIENTS_LIST_URL).mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "30"}
                )
            )
            with pytest.raises(ConnectorRateLimited) as excinfo:
                await client.list_clients()
            assert excinfo.value.retry_after == 30.0

    _run_with_firm(sm, firm_id, body)


def test_list_clients_5xx_raises_transient(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lc5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_CLIENTS_LIST_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(ConnectorTransient):
                await client.list_clients()

    _run_with_firm(sm, firm_id, body)


def test_list_clients_network_error_raises_transient_and_audits(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lcnet-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_CLIENTS_LIST_URL).mock(
                side_effect=httpx.ConnectError("no net")
            )
            with pytest.raises(ConnectorTransient):
                await client.list_clients()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.clients.list_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


def test_list_clients_rejects_naive_updated_since(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-naive-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ValueError):
            await client.list_clients(updated_since=_dt.datetime(2024, 1, 1))

    _run_with_firm(sm, firm_id, body)


def test_list_clients_missing_xpm_account_id_raises_auth_error(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-notenant-{uuid.uuid4().hex[:8]}",
        xpm_access_token="cached",
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
        xpm_account_id=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ConnectorAuthError, match="xpm_account_id"):
            await client.list_clients()

    _run_with_firm(sm, firm_id, body)


# =========================================================================
# get_client
# =========================================================================


def test_get_client_returns_parsed_record_and_audits(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-gc-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_CLIENT_GET_URL_PREFIX}/c-1").mock(
                return_value=httpx.Response(
                    200, json={"Client": _client_payload(cid="c-1")}
                )
            )
            return await client.get_client("c-1")

    record = _run_with_firm(sm, firm_id, body)
    assert isinstance(record, XPMClientRecord)
    assert record.id == "c-1"
    assert record.name == "Acme Pty Ltd"

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.clients.get"]
    assert len(success) == 1
    assert success[0].payload["client_id"] == "c-1"


def test_get_client_handles_bare_object_envelope(xpm_environment) -> None:
    """Some XPM endpoints return the record directly without an outer key."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-gcb-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_CLIENT_GET_URL_PREFIX}/c-1").mock(
                return_value=httpx.Response(200, json=_client_payload(cid="c-1"))
            )
            return await client.get_client("c-1")

    record = _run_with_firm(sm, firm_id, body)
    assert record.id == "c-1"


def test_get_client_404_raises_not_found(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-gc404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_CLIENT_GET_URL_PREFIX}/missing").mock(
                return_value=httpx.Response(404)
            )
            with pytest.raises(ConnectorNotFound):
                await client.get_client("missing")

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.clients.get_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_404"
    assert failed[0].payload["client_id"] == "missing"


def test_get_client_percent_encodes_id_with_special_chars(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-gcurl-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    cid = "id/with=slashes"

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(
                url__regex=(
                    r"^https://api\.xero\.com/practicemanager/3\.1/"
                    r"client\.api/get/[^/]+$"
                )
            ).mock(
                return_value=httpx.Response(200, json=_client_payload(cid=cid))
            )
            await client.get_client(cid)
        sent_url = str(route.calls.last.request.url)
        assert "with/slashes" not in sent_url
        assert "%2F" in sent_url
        assert "%3D" in sent_url

    _run_with_firm(sm, firm_id, body)


def test_get_client_rejects_empty_id(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-gce-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ValueError):
            await client.get_client("")

    _run_with_firm(sm, firm_id, body)


# =========================================================================
# list_jobs / list_invoices / get_invoice / list_relationships
# =========================================================================


_JOBS_LIST_URL = "https://api.xero.com/practicemanager/3.1/jobs.api/list"
_INVOICES_LIST_URL = "https://api.xero.com/practicemanager/3.1/invoices.api/list"
_INVOICE_GET_URL_PREFIX = (
    "https://api.xero.com/practicemanager/3.1/invoice.api/get"
)
_RELATIONSHIPS_LIST_URL = (
    "https://api.xero.com/practicemanager/3.1/relationships.api/list"
)


def _job_payload(
    *,
    jid: str = "j-1",
    name: str = "FY25 Tax Return",
    client_id: str = "c-1",
    state: str = "In Progress",
    start: str = "2025-07-01T09:00:00",
    due: str | None = "2025-10-31T17:00:00",
    completed: str | None = None,
) -> dict:
    out: dict = {
        "ID": jid,
        "Name": name,
        "ClientID": client_id,
        "State": state,
        "StartDate": start,
    }
    if due is not None:
        out["DueDate"] = due
    if completed is not None:
        out["CompletedDate"] = completed
    return out


def _invoice_payload(
    *,
    iid: str = "inv-1",
    number: str = "INV-0001",
    client_id: str = "c-1",
    total: str = "1100.00",
    tax: str = "100.00",
    currency: str = "AUD",
    status: str = "Sent",
    date: str = "2025-05-01T10:00:00",
    due: str | None = "2025-05-31T10:00:00",
) -> dict:
    out: dict = {
        "ID": iid,
        "InvoiceNumber": number,
        "ClientID": client_id,
        "TotalAmount": total,
        "TotalTax": tax,
        "Currency": currency,
        "Status": status,
        "Date": date,
    }
    if due is not None:
        out["DueDate"] = due
    return out


def _relationship_payload(
    *,
    rid: str = "r-1",
    from_id: str = "c-1",
    to_id: str = "c-2",
    rel_type: str = "Director",
    is_active: bool = True,
) -> dict:
    return {
        "ID": rid,
        "FromClientID": from_id,
        "ToClientID": to_id,
        "Type": rel_type,
        "IsActive": is_active,
    }


def test_list_jobs_returns_parsed_records(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lj-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_JOBS_LIST_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"Jobs": [_job_payload(jid="j-1"), _job_payload(
                        jid="j-2", state="Complete",
                        completed="2025-10-30T16:00:00",
                    )]},
                )
            )
            return await client.list_jobs()

    jobs = _run_with_firm(sm, firm_id, body)
    assert len(jobs) == 2
    assert all(isinstance(j, XPMJob) for j in jobs)
    assert jobs[0].state == "In Progress"
    assert jobs[0].due_at is not None
    assert jobs[0].completed_at is None
    assert jobs[1].completed_at is not None
    assert jobs[1].state == "Complete"

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.jobs.list"]
    assert len(success) == 1
    assert success[0].payload["count"] == 2


def test_list_jobs_scoped_to_client(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-ljc-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_JOBS_LIST_URL).mock(
                return_value=httpx.Response(200, json={"Jobs": []})
            )
            await client.list_jobs(client_id="c-42")
        sent = route.calls.last.request
        assert sent.url.params["clientid"] == "c-42"

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.jobs.list"]
    assert success[0].payload["client_id"] == "c-42"


def test_list_jobs_rejects_empty_client_id(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lje-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ValueError):
            await client.list_jobs(client_id="")

    _run_with_firm(sm, firm_id, body)


def test_list_invoices_returns_parsed_records_with_decimal_amounts(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-li-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_INVOICES_LIST_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "Invoices": [
                            _invoice_payload(
                                iid="inv-1",
                                total="1234.56",
                                tax="112.23",
                            )
                        ]
                    },
                )
            )
            return await client.list_invoices()

    invoices = _run_with_firm(sm, firm_id, body)
    assert len(invoices) == 1
    inv = invoices[0]
    assert isinstance(inv, XPMInvoice)
    assert inv.id == "inv-1"
    # Decimal-precise — no binary-float artefacts.
    assert inv.total_amount == Decimal("1234.56")
    assert inv.total_tax == Decimal("112.23")
    assert inv.currency == "AUD"
    assert inv.due_at is not None


def test_list_invoices_scoped_to_client(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lic-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_INVOICES_LIST_URL).mock(
                return_value=httpx.Response(200, json={"Invoices": []})
            )
            await client.list_invoices(client_id="c-7")
        assert route.calls.last.request.url.params["clientid"] == "c-7"

    _run_with_firm(sm, firm_id, body)


def test_get_invoice_404_raises_not_found(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-gi404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_INVOICE_GET_URL_PREFIX}/missing").mock(
                return_value=httpx.Response(404)
            )
            with pytest.raises(ConnectorNotFound):
                await client.get_invoice("missing")

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.invoices.get_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_404"
    assert failed[0].payload["invoice_id"] == "missing"


def test_get_invoice_happy_path(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-gi-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_INVOICE_GET_URL_PREFIX}/inv-1").mock(
                return_value=httpx.Response(
                    200, json={"Invoice": _invoice_payload(iid="inv-1")}
                )
            )
            return await client.get_invoice("inv-1")

    inv = _run_with_firm(sm, firm_id, body)
    assert inv.id == "inv-1"
    assert inv.number == "INV-0001"


def test_list_relationships_returns_directed_edges(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lr-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_RELATIONSHIPS_LIST_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "Relationships": [
                            _relationship_payload(
                                rid="r-1", rel_type="Director"
                            ),
                            _relationship_payload(
                                rid="r-2",
                                from_id="c-1",
                                to_id="c-3",
                                rel_type="Trustee",
                                is_active=False,
                            ),
                        ]
                    },
                )
            )
            rels = await client.list_relationships("c-1")
        assert route.calls.last.request.url.params["clientid"] == "c-1"
        return rels

    rels = _run_with_firm(sm, firm_id, body)
    assert len(rels) == 2
    assert all(isinstance(r, XPMRelationship) for r in rels)
    assert rels[0].relationship_type == "Director"
    assert rels[0].is_active is True
    assert rels[1].relationship_type == "Trustee"
    assert rels[1].is_active is False

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.relationships.list"]
    assert len(success) == 1
    assert success[0].payload["client_id"] == "c-1"
    assert success[0].payload["count"] == 2


def test_list_relationships_rejects_empty_client_id(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-lre-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ValueError):
            await client.list_relationships("")

    _run_with_firm(sm, firm_id, body)


# =========================================================================
# Pagination — Link: rel="next"
# =========================================================================


def _page_response_callback(*, mapping: dict[str, dict]):
    """Return a respx side_effect that dispatches by exact URL match.

    Required because respx's default route matching is path-based,
    so multiple mocks with the same path (different query strings)
    collide. A single side_effect dispatched by str(request.url) is
    unambiguous.

    ``mapping`` values are response specs (dicts with status / headers
    / json), not pre-built Response objects, so each call gets a
    fresh ``httpx.Response`` whose body hasn't been consumed.
    """

    def _dispatch(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        spec = mapping.get(url)
        if spec is not None:
            return httpx.Response(**spec)
        return httpx.Response(
            418,
            json={"error": f"unmocked URL in pagination test: {url}"},
        )

    return _dispatch


def test_list_clients_follows_link_rel_next_across_pages(
    xpm_environment,
) -> None:
    """Three-page result: 2 + 2 + 1 = 5 records. Single audit row,
    count=5, pages=3.
    """
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-pag-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    page2_url = f"{_CLIENTS_LIST_URL}?page=2"
    page3_url = f"{_CLIENTS_LIST_URL}?page=3"

    dispatcher = _page_response_callback(
        mapping={
            _CLIENTS_LIST_URL: {
                "status_code": 200,
                "headers": {"Link": f'<{page2_url}>; rel="next"'},
                "json": {
                    "Clients": [
                        _client_payload(cid="c-1"),
                        _client_payload(cid="c-2"),
                    ]
                },
            },
            page2_url: {
                "status_code": 200,
                "headers": {
                    "Link": (
                        f'<{page3_url}>; rel="next", '
                        f'<{_CLIENTS_LIST_URL}>; rel="prev"'
                    )
                },
                "json": {
                    "Clients": [
                        _client_payload(cid="c-3"),
                        _client_payload(cid="c-4"),
                    ]
                },
            },
            page3_url: {
                "status_code": 200,
                "json": {"Clients": [_client_payload(cid="c-5")]},
            },
        }
    )

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(
                url__regex=(
                    r"^https://api\.xero\.com/practicemanager/3\.1/clients\.api/list"
                )
            ).mock(side_effect=dispatcher)
            return await client.list_clients()

    clients = _run_with_firm(sm, firm_id, body)
    assert [c.id for c in clients] == ["c-1", "c-2", "c-3", "c-4", "c-5"]

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.clients.list"]
    assert len(success) == 1
    assert success[0].payload["count"] == 5
    assert success[0].payload["pages"] == 3


def test_pagination_failure_on_second_page_audits_once_and_raises(
    xpm_environment,
) -> None:
    """If page 2 returns 5xx, no success audit; one failure audit."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_with_valid_token(sm, slug=f"xpm-pagfail-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    page2_url = f"{_CLIENTS_LIST_URL}?page=2"

    dispatcher = _page_response_callback(
        mapping={
            _CLIENTS_LIST_URL: {
                "status_code": 200,
                "headers": {"Link": f'<{page2_url}>; rel="next"'},
                "json": {"Clients": [_client_payload(cid="c-1")]},
            },
            page2_url: {"status_code": 503},
        }
    )

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(
                url__regex=(
                    r"^https://api\.xero\.com/practicemanager/3\.1/clients\.api/list"
                )
            ).mock(side_effect=dispatcher)
            with pytest.raises(ConnectorTransient):
                await client.list_clients()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    assert not any(a.action == "xpm.clients.list" for a in audits)
    failed = [a for a in audits if a.action == "xpm.clients.list_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_5xx"
