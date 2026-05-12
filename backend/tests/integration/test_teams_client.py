"""Integration tests for ``coworker.connectors.teams_client``."""
import asyncio
import uuid

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
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.connectors.shadow_mode import ShadowModeBlocked
from coworker.connectors.teams_client import TeamsClient, TeamsMessage
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import encrypt_str

_WEBHOOK_URL = "https://example.webhook.office.com/webhookb2/abc123"


@pytest.fixture
def teams_environment(test_database_url):
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
    webhook_url: str | None = _WEBHOOK_URL,
    shadow_mode: bool = False,
) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            kwargs: dict = {
                "id": firm_id,
                "name": "Teams Test Firm",
                "slug": slug,
                "shadow_mode": shadow_mode,
            }
            if webhook_url is not None:
                kwargs["teams_webhook_url_ciphertext"] = encrypt_str(
                    webhook_url, firm_id=firm_id_str
                )
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


def _run_with_firm(sessionmaker, firm_id, body):
    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            return await body(session, firm)

    return asyncio.run(_run())


# =========================================================================
# send_message
# =========================================================================


def test_send_message_posts_message_card_and_audits(teams_environment) -> None:
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"tm-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_WEBHOOK_URL).mock(
                return_value=httpx.Response(200, text="1")
            )
            await client.send_message(
                TeamsMessage(
                    text="Daily briefing ready",
                    title="Morning Briefing",
                    theme_color="0078D4",
                )
            )
        sent_body = route.calls.last.request.read().decode()
        assert '"@type": "MessageCard"' in sent_body or '"@type":"MessageCard"' in sent_body
        assert "Daily briefing ready" in sent_body
        assert "Morning Briefing" in sent_body
        assert "0078D4" in sent_body

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "teams.send_message"]
    assert len(success) == 1
    assert success[0].payload["title"] == "Morning Briefing"
    assert success[0].payload["text_length"] == len("Daily briefing ready")
    # Text itself never enters audit
    assert "Daily briefing ready" not in str(success[0].payload)


def test_send_message_in_shadow_mode_blocks_with_no_http(teams_environment) -> None:
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"tm-shadow-{uuid.uuid4().hex[:8]}", shadow_mode=True)
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with respx.mock():
            with pytest.raises(ShadowModeBlocked) as excinfo:
                await client.send_message(TeamsMessage(text="should not send"))
            assert excinfo.value.action == "teams.send_message"

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    assert not any(a.action == "teams.send_message" for a in audits)
    blocked = [a for a in audits if a.action == "shadow_blocked.teams.send_message"]
    assert len(blocked) == 1


def test_send_message_410_gone_raises_auth_error_and_audits(
    teams_environment,
) -> None:
    """A deleted webhook returns 410 Gone — same operational meaning as 401."""
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"tm-410-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_WEBHOOK_URL).mock(return_value=httpx.Response(410))
            with pytest.raises(ConnectorAuthError):
                await client.send_message(TeamsMessage(text="x"))

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "teams.send_message_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "teams_410"


def test_send_message_429_with_retry_after(teams_environment) -> None:
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"tm-429-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_WEBHOOK_URL).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "5"})
            )
            with pytest.raises(ConnectorRateLimited) as excinfo:
                await client.send_message(TeamsMessage(text="x"))
            assert excinfo.value.retry_after == 5.0

    _run_with_firm(sm, firm_id, body)


def test_send_message_5xx_raises_transient(teams_environment) -> None:
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"tm-5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_WEBHOOK_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(ConnectorTransient):
                await client.send_message(TeamsMessage(text="x"))

    _run_with_firm(sm, firm_id, body)


def test_send_message_network_error_raises_transient_and_audits(
    teams_environment,
) -> None:
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"tm-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_WEBHOOK_URL).mock(side_effect=httpx.ConnectError("no net"))
            with pytest.raises(ConnectorTransient):
                await client.send_message(TeamsMessage(text="x"))

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "teams.send_message_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


def test_send_message_missing_webhook_url_raises_auth_error(
    teams_environment,
) -> None:
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm, slug=f"tm-nohook-{uuid.uuid4().hex[:8]}", webhook_url=None
    )
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with pytest.raises(ConnectorAuthError, match="teams_webhook_url"):
            await client.send_message(TeamsMessage(text="x"))

    _run_with_firm(sm, firm_id, body)


def test_send_message_rejects_empty_text(teams_environment) -> None:
    sm = teams_environment["sessionmaker"]
    created = teams_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"tm-empty-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = TeamsClient(firm, session=session)
        with pytest.raises(ValueError):
            await client.send_message(TeamsMessage(text=""))

    _run_with_firm(sm, firm_id, body)
