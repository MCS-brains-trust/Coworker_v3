"""Integration tests for `coworker.graph.mail.list_inbox`.

Pattern matches `test_graph_auth.py`: direct call into the helper
under firm_context, Microsoft Graph mocked via respx, real DB.

Each test seeds a firm + user, builds a GraphContext directly (no
FastAPI dependency machinery — that's covered by
`test_graph_context.py`), calls `list_inbox`, and asserts on both
the return value and the audit chain.
"""
import asyncio
import datetime as _dt
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
    ConnectorNotFound,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.connectors.shadow_mode import ShadowModeBlocked
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.context import GraphContext
from coworker.graph.mail import (
    EmailAttachment,
    FullEmailMessage,
    InboxAddress,
    InboxMessage,
    create_draft,
    get_attachment,
    get_message,
    list_inbox,
)
from coworker.security.encryption import encrypt_str

_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def graph_mail_environment(test_database_url):
    """NullPool engine + sessionmaker for direct helper calls."""
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
                text("DELETE FROM users WHERE firm_id = :id"), {"id": str(firm_id)}
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


def _seed(
    sessionmaker, *, slug: str, shadow_mode: bool = False
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a minimal firm + user. Returns (firm_id, user_id).

    ``shadow_mode`` defaults to False so the seeded firm can exercise
    write paths in tests; the production Firm default remains True
    (a firm is in shadow mode until the principal explicitly graduates).
    """

    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Mail Test Firm",
                    slug=slug,
                    shadow_mode=shadow_mode,
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
                upn=f"mail-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Mail Test User",
                ms_access_token_ciphertext=encrypt_str(
                    "test-access", firm_id=firm_id_str
                ),
                ms_refresh_token_ciphertext=encrypt_str(
                    "test-refresh", firm_id=firm_id_str
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


def _sample_graph_message(
    *,
    msg_id: str,
    subject: str,
    from_email: str | None = "alice@example.com",
    from_name: str | None = "Alice Smith",
    received: str = "2026-05-08T10:00:00Z",
    preview: str = "Hi there",
    is_read: bool = False,
    has_attachments: bool = False,
) -> dict:
    """Construct a Graph-shaped message JSON dict."""
    msg: dict = {
        "id": msg_id,
        "subject": subject,
        "receivedDateTime": received,
        "bodyPreview": preview,
        "isRead": is_read,
        "hasAttachments": has_attachments,
    }
    if from_email is not None:
        msg["from"] = {"emailAddress": {"address": from_email, "name": from_name}}
    return msg


# --------------------------- happy paths ------------------------------------


def test_list_inbox_returns_parsed_messages_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    messages_payload = [
        _sample_graph_message(
            msg_id=f"msg-{i}",
            subject=f"Subject {i}",
            received=f"2026-05-08T{10 + i:02d}:00:00Z",
            is_read=(i % 2 == 0),
            has_attachments=(i == 0),
        )
        for i in range(3)
    ]

    async def _run() -> list[InboxMessage]:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm,
                user=user,
                access_token="bearer-token-xyz",
                session=session,
            )

            with respx.mock(assert_all_called=True) as rmock:
                route = rmock.get(_GRAPH_MESSAGES_URL).mock(
                    return_value=httpx.Response(
                        200, json={"value": messages_payload}
                    )
                )
                returned = await list_inbox(ctx, top=3)

            # Verify the request shape.
            assert route.called
            sent = route.calls.last.request
            assert sent.headers["Authorization"] == "Bearer bearer-token-xyz"
            assert sent.url.params["$top"] == "3"
            assert sent.url.params["$orderby"] == "receivedDateTime desc"
            return returned

    result = asyncio.run(_run())

    assert len(result) == 3
    assert all(isinstance(m, InboxMessage) for m in result)
    assert result[0].id == "msg-0"
    assert result[0].subject == "Subject 0"
    assert result[0].sender == InboxAddress(
        email="alice@example.com", name="Alice Smith"
    )
    assert result[0].is_read is True
    assert result[0].has_attachments is True
    assert result[0].received_at.tzinfo is not None  # tz-aware

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.mail.list_inbox"]
    assert len(success) == 1
    assert success[0].payload["count"] == 3
    assert success[0].payload["top"] == 3
    assert success[0].payload["user_id"] == str(user_id)


def test_list_inbox_empty_returns_empty_list(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-empty-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> list[InboxMessage]:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )

            with respx.mock(assert_all_called=True) as rmock:
                rmock.get(_GRAPH_MESSAGES_URL).mock(
                    return_value=httpx.Response(200, json={"value": []})
                )
                return await list_inbox(ctx)

    result = asyncio.run(_run())
    assert result == []

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.mail.list_inbox"]
    assert len(success) == 1
    assert success[0].payload["count"] == 0


def test_list_inbox_handles_message_without_sender(
    graph_mail_environment,
) -> None:
    """Some Graph messages (drafts, calendar) have no `from` field."""
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-nofrom-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> list[InboxMessage]:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )

            with respx.mock(assert_all_called=True) as rmock:
                rmock.get(_GRAPH_MESSAGES_URL).mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "value": [
                                _sample_graph_message(
                                    msg_id="m1", subject="No-from msg",
                                    from_email=None,
                                ),
                            ]
                        },
                    )
                )
                return await list_inbox(ctx)

    result = asyncio.run(_run())
    assert len(result) == 1
    assert result[0].sender is None


# --------------------------- failure paths ----------------------------------


def test_list_inbox_401_raises_auth_error_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )

            with respx.mock(assert_all_called=True) as rmock:
                rmock.get(_GRAPH_MESSAGES_URL).mock(
                    return_value=httpx.Response(401, json={"error": "unauthorized"})
                )
                with pytest.raises(ConnectorAuthError):
                    await list_inbox(ctx)

    asyncio.run(_run())

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.list_inbox_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_401"
    assert not any(a.action == "graph.mail.list_inbox" for a in audits)


def test_list_inbox_429_raises_rate_limited_with_retry_after(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-429-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )

            with respx.mock(assert_all_called=True) as rmock:
                rmock.get(_GRAPH_MESSAGES_URL).mock(
                    return_value=httpx.Response(
                        429,
                        headers={"Retry-After": "42"},
                        json={"error": "throttled"},
                    )
                )
                with pytest.raises(ConnectorRateLimited) as excinfo:
                    await list_inbox(ctx)
                assert excinfo.value.retry_after == 42.0

    asyncio.run(_run())

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.list_inbox_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_429"


def test_list_inbox_429_without_retry_after(graph_mail_environment) -> None:
    """Missing or non-numeric Retry-After ⇒ retry_after=None."""
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-429b-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )

            with respx.mock(assert_all_called=True) as rmock:
                rmock.get(_GRAPH_MESSAGES_URL).mock(
                    return_value=httpx.Response(429, json={"error": "throttled"})
                )
                with pytest.raises(ConnectorRateLimited) as excinfo:
                    await list_inbox(ctx)
                assert excinfo.value.retry_after is None

    asyncio.run(_run())


def test_list_inbox_5xx_raises_transient_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )

            with respx.mock(assert_all_called=True) as rmock:
                rmock.get(_GRAPH_MESSAGES_URL).mock(
                    return_value=httpx.Response(503)
                )
                with pytest.raises(ConnectorTransient):
                    await list_inbox(ctx)

    asyncio.run(_run())

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.list_inbox_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_5xx"


def test_list_inbox_network_error_raises_transient_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )

            with respx.mock(assert_all_called=True) as rmock:
                rmock.get(_GRAPH_MESSAGES_URL).mock(
                    side_effect=httpx.ConnectError("no network")
                )
                with pytest.raises(ConnectorTransient):
                    await list_inbox(ctx)

    asyncio.run(_run())

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.list_inbox_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


# --------------------------- input validation -------------------------------


def test_list_inbox_rejects_invalid_top(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-top-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="x", session=session
            )
            with pytest.raises(ValueError):
                await list_inbox(ctx, top=0)
            with pytest.raises(ValueError):
                await list_inbox(ctx, top=-5)
            with pytest.raises(ValueError):
                await list_inbox(ctx, top=1001)

    asyncio.run(_run())


# =========================================================================
# get_message
# =========================================================================


def _full_graph_message(
    *,
    msg_id: str = "AAMkADk-msg-1",
    subject: str = "Quarterly BAS",
    from_email: str | None = "alice@example.com",
    from_name: str | None = "Alice Smith",
    to: list[tuple[str, str | None]] | None = None,
    cc: list[tuple[str, str | None]] | None = None,
    bcc: list[tuple[str, str | None]] | None = None,
    received: str = "2026-05-08T10:00:00Z",
    body_type: str = "html",
    body_content: str = "<p>Body</p>",
    is_read: bool = False,
    has_attachments: bool = False,
    conversation_id: str | None = "conv-1",
) -> dict:
    """Construct a Graph-shaped full-message JSON dict for /me/messages/{id}."""

    def _addr(email: str, name: str | None) -> dict:
        return {"emailAddress": {"address": email, "name": name}}

    msg: dict = {
        "id": msg_id,
        "subject": subject,
        "receivedDateTime": received,
        "body": {"contentType": body_type, "content": body_content},
        "isRead": is_read,
        "hasAttachments": has_attachments,
        "conversationId": conversation_id,
        "toRecipients": [_addr(e, n) for (e, n) in (to or [])],
        "ccRecipients": [_addr(e, n) for (e, n) in (cc or [])],
        "bccRecipients": [_addr(e, n) for (e, n) in (bcc or [])],
    }
    if from_email is not None:
        msg["from"] = {"emailAddress": {"address": from_email, "name": from_name}}
    return msg


def _run_with_ctx(sessionmaker, firm_id, user_id, body):
    """Helper: build a GraphContext bound to the seeded firm/user and run body(ctx)."""

    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="bearer-xyz", session=session
            )
            return await body(ctx)

    return asyncio.run(_run())


def test_get_message_returns_full_message_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-get-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    msg_id = "AAMkADk-msg-42"
    payload = _full_graph_message(
        msg_id=msg_id,
        to=[("bob@example.com", "Bob Jones")],
        cc=[("carol@example.com", None)],
        has_attachments=True,
    )

    async def body(ctx: GraphContext) -> FullEmailMessage:
        url = f"{_GRAPH_MESSAGES_URL}/{msg_id}"
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(url).mock(
                return_value=httpx.Response(200, json=payload)
            )
            result = await get_message(ctx, msg_id)
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer bearer-xyz"
        assert "body" in sent.url.params["$select"]
        assert "toRecipients" in sent.url.params["$select"]
        return result

    result = _run_with_ctx(sm, firm_id, user_id, body)

    assert isinstance(result, FullEmailMessage)
    assert result.id == msg_id
    assert result.subject == "Quarterly BAS"
    assert result.sender == InboxAddress(
        email="alice@example.com", name="Alice Smith"
    )
    assert result.to_recipients == [
        InboxAddress(email="bob@example.com", name="Bob Jones")
    ]
    assert result.cc_recipients == [InboxAddress(email="carol@example.com")]
    assert result.bcc_recipients == []
    assert result.body.content_type == "html"
    assert result.body.content == "<p>Body</p>"
    assert result.has_attachments is True
    assert result.conversation_id == "conv-1"
    assert result.received_at.tzinfo is not None

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.mail.get_message"]
    assert len(success) == 1
    assert success[0].payload["message_id"] == msg_id
    assert success[0].payload["has_attachments"] is True
    assert success[0].payload["user_id"] == str(user_id)


def test_get_message_normalises_uppercase_body_content_type(
    graph_mail_environment,
) -> None:
    """Graph occasionally returns ``contentType: HTML`` — normalise to lowercase."""
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-bodyct-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    msg_id = "msg-mixedcase"
    payload = _full_graph_message(msg_id=msg_id, body_type="HTML")

    async def body(ctx: GraphContext) -> FullEmailMessage:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/{msg_id}").mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await get_message(ctx, msg_id)

    result = _run_with_ctx(sm, firm_id, user_id, body)
    assert result.body.content_type == "html"


def test_get_message_percent_encodes_id_with_special_chars(
    graph_mail_environment,
) -> None:
    """Message ids containing `/` or `=` must be percent-encoded in the URL."""
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-encode-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    msg_id = "AAMk/ADk=msg/with/slashes"

    async def body(ctx: GraphContext) -> FullEmailMessage:
        # Mock with a regex so we can assert the URL was percent-encoded.
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(
                url__regex=r"^https://graph\.microsoft\.com/v1\.0/me/messages/[^/]+$"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json=_full_graph_message(msg_id=msg_id),
                )
            )
            result = await get_message(ctx, msg_id)
        sent = route.calls.last.request
        # Raw / and = must be encoded — confirm no literal "/with/" in path.
        assert "with/slashes" not in str(sent.url)
        assert "%2F" in str(sent.url)
        assert "%3D" in str(sent.url)
        return result

    _run_with_ctx(sm, firm_id, user_id, body)


def test_get_message_404_raises_not_found_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-get404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    msg_id = "msg-missing"

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/{msg_id}").mock(
                return_value=httpx.Response(404, json={"error": "not found"})
            )
            with pytest.raises(ConnectorNotFound):
                await get_message(ctx, msg_id)

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.get_message_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_404"
    assert failed[0].payload["message_id"] == msg_id


def test_get_message_401_raises_auth_error_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-get401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    msg_id = "msg-1"

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/{msg_id}").mock(
                return_value=httpx.Response(401, json={"error": "unauthorized"})
            )
            with pytest.raises(ConnectorAuthError):
                await get_message(ctx, msg_id)

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.get_message_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_401"


def test_get_message_429_with_retry_after(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-get429-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1").mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "13"}, json={"error": "throttled"}
                )
            )
            with pytest.raises(ConnectorRateLimited) as excinfo:
                await get_message(ctx, "m1")
            assert excinfo.value.retry_after == 13.0

    _run_with_ctx(sm, firm_id, user_id, body)


def test_get_message_5xx_raises_transient(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-get5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1").mock(
                return_value=httpx.Response(503)
            )
            with pytest.raises(ConnectorTransient):
                await get_message(ctx, "m1")

    _run_with_ctx(sm, firm_id, user_id, body)


def test_get_message_network_error_raises_transient_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-getnet-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1").mock(
                side_effect=httpx.ConnectError("no network")
            )
            with pytest.raises(ConnectorTransient):
                await get_message(ctx, "m1")

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.get_message_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"
    assert failed[0].payload["message_id"] == "m1"


def test_get_message_rejects_empty_id(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"mail-getempty-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await get_message(ctx, "")

    _run_with_ctx(sm, firm_id, user_id, body)


# =========================================================================
# get_attachment
# =========================================================================


def _file_attachment(
    *,
    attachment_id: str = "att-1",
    name: str = "tax_return.pdf",
    content_type: str = "application/pdf",
    content: bytes = b"%PDF-1.4 test content",
    is_inline: bool = False,
) -> dict:
    import base64 as _b64

    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": attachment_id,
        "name": name,
        "contentType": content_type,
        "size": len(content),
        "isInline": is_inline,
        "lastModifiedDateTime": "2026-05-01T10:00:00Z",
        "contentBytes": _b64.b64encode(content).decode("ascii"),
    }


def test_get_attachment_returns_file_with_decoded_bytes_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    content = b"%PDF-1.4 hello world"
    payload = _file_attachment(content=content)

    async def body(ctx: GraphContext) -> EmailAttachment:
        url = f"{_GRAPH_MESSAGES_URL}/msg-1/attachments/att-1"
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(url).mock(
                return_value=httpx.Response(200, json=payload)
            )
            result = await get_attachment(ctx, "msg-1", "att-1")
        assert route.called
        assert route.calls.last.request.headers["Authorization"] == "Bearer bearer-xyz"
        return result

    result = _run_with_ctx(sm, firm_id, user_id, body)

    assert isinstance(result, EmailAttachment)
    assert result.id == "att-1"
    assert result.attachment_type == "file"
    assert result.name == "tax_return.pdf"
    assert result.content_type == "application/pdf"
    assert result.size == len(content)
    assert result.is_inline is False
    assert result.content == content

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.mail.get_attachment"]
    assert len(success) == 1
    assert success[0].payload["message_id"] == "msg-1"
    assert success[0].payload["attachment_id"] == "att-1"
    assert success[0].payload["attachment_type"] == "file"
    assert success[0].payload["size"] == len(content)


def test_get_attachment_item_type_has_none_content(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-item-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "@odata.type": "#microsoft.graph.itemAttachment",
        "id": "att-item",
        "name": "Forwarded message",
        "contentType": None,
        "size": 4321,
        "isInline": False,
    }

    async def body(ctx: GraphContext) -> EmailAttachment:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1/attachments/att-item").mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await get_attachment(ctx, "m1", "att-item")

    result = _run_with_ctx(sm, firm_id, user_id, body)
    assert result.attachment_type == "item"
    assert result.content is None
    assert result.size == 4321


def test_get_attachment_reference_type_has_none_content(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-ref-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "@odata.type": "#microsoft.graph.referenceAttachment",
        "id": "att-ref",
        "name": "Quarterly report",
        "contentType": None,
        "size": 0,
        "isInline": False,
    }

    async def body(ctx: GraphContext) -> EmailAttachment:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1/attachments/att-ref").mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await get_attachment(ctx, "m1", "att-ref")

    result = _run_with_ctx(sm, firm_id, user_id, body)
    assert result.attachment_type == "reference"
    assert result.content is None


def test_get_attachment_unknown_odata_type_returns_unknown(
    graph_mail_environment,
) -> None:
    """Graph occasionally invents new attachment types — surface as 'unknown'."""
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-unknown-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "@odata.type": "#microsoft.graph.futureWeirdAttachment",
        "id": "att-x",
        "name": "weird",
        "contentType": "application/octet-stream",
        "size": 0,
        "isInline": False,
    }

    async def body(ctx: GraphContext) -> EmailAttachment:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1/attachments/att-x").mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await get_attachment(ctx, "m1", "att-x")

    result = _run_with_ctx(sm, firm_id, user_id, body)
    assert result.attachment_type == "unknown"
    assert result.content is None


def test_get_attachment_invalid_base64_raises_value_error(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-b64-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": "att-bad",
        "name": "broken.bin",
        "contentType": "application/octet-stream",
        "size": 4,
        "isInline": False,
        "contentBytes": "!!!not-valid-base64!!!",
    }

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1/attachments/att-bad").mock(
                return_value=httpx.Response(200, json=payload)
            )
            with pytest.raises(ValueError):
                await get_attachment(ctx, "m1", "att-bad")

    _run_with_ctx(sm, firm_id, user_id, body)


def test_get_attachment_404_raises_not_found(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1/attachments/missing").mock(
                return_value=httpx.Response(404, json={"error": "not found"})
            )
            with pytest.raises(ConnectorNotFound):
                await get_attachment(ctx, "m1", "missing")

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.get_attachment_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_404"
    assert failed[0].payload["message_id"] == "m1"
    assert failed[0].payload["attachment_id"] == "missing"


def test_get_attachment_network_error_raises_transient(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/m1/attachments/a1").mock(
                side_effect=httpx.ConnectError("no network")
            )
            with pytest.raises(ConnectorTransient):
                await get_attachment(ctx, "m1", "a1")

    _run_with_ctx(sm, firm_id, user_id, body)


def test_get_attachment_rejects_empty_ids(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"att-empty-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await get_attachment(ctx, "", "a1")
        with pytest.raises(ValueError):
            await get_attachment(ctx, "m1", "")

    _run_with_ctx(sm, firm_id, user_id, body)


# =========================================================================
# create_draft
# =========================================================================


def _draft_response(
    *,
    draft_id: str = "draft-1",
    subject: str = "Re: Quarterly BAS",
) -> dict:
    """Graph-shaped response to POST /me/messages or PATCH /me/messages/{id}."""
    return {
        "id": draft_id,
        "subject": subject,
        "receivedDateTime": "2026-05-12T08:00:00Z",
        "body": {"contentType": "html", "content": "<p>draft body</p>"},
        "isRead": True,
        "hasAttachments": False,
        "conversationId": "conv-1",
        "toRecipients": [
            {"emailAddress": {"address": "alice@example.com", "name": "Alice"}}
        ],
        "ccRecipients": [],
        "bccRecipients": [],
        "from": {"emailAddress": {"address": "me@example.com"}},
    }


def test_create_draft_new_returns_message_and_audits(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-new-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> FullEmailMessage:
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(
                    201, json=_draft_response(draft_id="new-draft-1")
                )
            )
            message = await create_draft(
                ctx,
                to=["alice@example.com"],
                subject="Hello",
                body="<p>Hi Alice</p>",
            )
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer bearer-xyz"
        assert sent.headers["Content-Type"] == "application/json"
        sent_payload = sent.read().decode()
        assert "alice@example.com" in sent_payload
        assert "Hi Alice" in sent_payload
        assert "ccRecipients" not in sent_payload
        return message

    message = _run_with_ctx(sm, firm_id, user_id, body)
    assert isinstance(message, FullEmailMessage)
    assert message.id == "new-draft-1"

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.mail.create_draft"]
    assert len(success) == 1
    assert success[0].payload["draft_id"] == "new-draft-1"
    assert success[0].payload["recipient_count"] == 1
    assert "in_reply_to" not in success[0].payload


def test_create_draft_with_in_reply_to_does_two_step(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-reply-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    orig_id = "orig-msg-42"
    reply_draft_id = "AAMk-reply-draft"

    async def body(ctx: GraphContext) -> FullEmailMessage:
        reply_url = f"{_GRAPH_MESSAGES_URL}/{orig_id}/createReply"
        patch_url = f"{_GRAPH_MESSAGES_URL}/{reply_draft_id}"
        with respx.mock(assert_all_called=True) as rmock:
            reply_route = rmock.post(reply_url).mock(
                return_value=httpx.Response(
                    201, json=_draft_response(draft_id=reply_draft_id)
                )
            )
            patch_route = rmock.patch(patch_url).mock(
                return_value=httpx.Response(
                    200, json=_draft_response(draft_id=reply_draft_id)
                )
            )
            message = await create_draft(
                ctx,
                to=["alice@example.com"],
                subject="Re: BAS",
                body="<p>Reply body</p>",
                in_reply_to=orig_id,
            )
        assert reply_route.called
        assert patch_route.called
        # PATCH carries the new content; createReply was empty
        patch_payload = patch_route.calls.last.request.read().decode()
        assert "Reply body" in patch_payload
        return message

    message = _run_with_ctx(sm, firm_id, user_id, body)
    assert message.id == reply_draft_id

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.mail.create_draft"]
    assert len(success) == 1
    assert success[0].payload["in_reply_to"] == orig_id
    assert success[0].payload["draft_id"] == reply_draft_id


def test_create_draft_includes_cc_and_bcc(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-cc-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(201, json=_draft_response())
            )
            await create_draft(
                ctx,
                to=["alice@example.com"],
                subject="Hi",
                body="hello",
                cc=["bob@example.com"],
                bcc=["carol@example.com"],
            )
        sent_payload = route.calls.last.request.read().decode()
        assert "alice@example.com" in sent_payload
        assert "bob@example.com" in sent_payload
        assert "carol@example.com" in sent_payload
        assert "ccRecipients" in sent_payload
        assert "bccRecipients" in sent_payload

    _run_with_ctx(sm, firm_id, user_id, body)


def test_create_draft_body_content_type_text(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-text-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(201, json=_draft_response())
            )
            await create_draft(
                ctx,
                to=["alice@example.com"],
                subject="Hi",
                body="plain text body",
                body_content_type="text",
            )
        sent_payload = route.calls.last.request.read().decode()
        assert '"contentType": "text"' in sent_payload or '"contentType":"text"' in sent_payload

    _run_with_ctx(sm, firm_id, user_id, body)


def test_create_draft_in_shadow_mode_blocks_with_no_http_call(
    graph_mail_environment,
) -> None:
    """guard_writable raises before any Graph call when firm.shadow_mode=True."""
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-shadow-{uuid.uuid4().hex[:8]}", shadow_mode=True
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        # respx.mock() with no configured routes asserts all requests
        # are mocked; if create_draft tried to make a Graph call, the
        # call would raise AllMockedAssertionError instead of
        # ShadowModeBlocked.
        with respx.mock() as rmock:  # noqa: F841
            with pytest.raises(ShadowModeBlocked) as excinfo:
                await create_draft(
                    ctx,
                    to=["alice@example.com"],
                    subject="Hi",
                    body="hello",
                )
            assert excinfo.value.action == "email.create_draft"

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    # No success audit; one shadow_blocked audit (from guard_writable).
    assert not any(a.action == "graph.mail.create_draft" for a in audits)
    blocked = [a for a in audits if a.action == "shadow_blocked.email.create_draft"]
    assert len(blocked) == 1
    assert blocked[0].payload["actor_id"] == str(user_id)


def test_create_draft_post_401_raises_auth_error_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-401-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(401, json={"error": "unauthorized"})
            )
            with pytest.raises(ConnectorAuthError):
                await create_draft(
                    ctx,
                    to=["alice@example.com"],
                    subject="Hi",
                    body="hello",
                )

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.create_draft_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_401"
    assert failed[0].payload["recipient_count"] == 1


def test_create_draft_post_5xx_raises_transient(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-5xx-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(503)
            )
            with pytest.raises(ConnectorTransient):
                await create_draft(
                    ctx,
                    to=["alice@example.com"],
                    subject="Hi",
                    body="hello",
                )

    _run_with_ctx(sm, firm_id, user_id, body)


def test_create_draft_429_with_retry_after(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-429-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "11"}, json={"error": "throttled"}
                )
            )
            with pytest.raises(ConnectorRateLimited) as excinfo:
                await create_draft(
                    ctx,
                    to=["alice@example.com"],
                    subject="Hi",
                    body="hello",
                )
            assert excinfo.value.retry_after == 11.0

    _run_with_ctx(sm, firm_id, user_id, body)


def test_create_draft_network_error_raises_transient_and_audits(
    graph_mail_environment,
) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-net-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_GRAPH_MESSAGES_URL).mock(
                side_effect=httpx.ConnectError("no network")
            )
            with pytest.raises(ConnectorTransient):
                await create_draft(
                    ctx,
                    to=["alice@example.com"],
                    subject="Hi",
                    body="hello",
                )

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.create_draft_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


def test_create_draft_reply_createreply_404(graph_mail_environment) -> None:
    """First step (createReply) 404 — original message was deleted."""
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-reply404-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    orig_id = "deleted-msg"

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(f"{_GRAPH_MESSAGES_URL}/{orig_id}/createReply").mock(
                return_value=httpx.Response(404, json={"error": "not found"})
            )
            with pytest.raises(ConnectorNotFound):
                await create_draft(
                    ctx,
                    to=["alice@example.com"],
                    subject="Hi",
                    body="hello",
                    in_reply_to=orig_id,
                )

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.create_draft_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_404"
    assert failed[0].payload["step"] == "createReply"
    assert failed[0].payload["in_reply_to"] == orig_id


def test_create_draft_reply_patch_5xx(graph_mail_environment) -> None:
    """Second step (PATCH) fails — createReply draft remains in Drafts.

    The audit row records ``step=patch_reply`` and the draft_id so a
    principal reading the chain can find the orphan and decide what
    to do with it.
    """
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-patch5xx-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    orig_id = "orig-msg"
    half_draft_id = "half-formed"

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(f"{_GRAPH_MESSAGES_URL}/{orig_id}/createReply").mock(
                return_value=httpx.Response(
                    201, json=_draft_response(draft_id=half_draft_id)
                )
            )
            rmock.patch(f"{_GRAPH_MESSAGES_URL}/{half_draft_id}").mock(
                return_value=httpx.Response(500)
            )
            with pytest.raises(ConnectorTransient):
                await create_draft(
                    ctx,
                    to=["alice@example.com"],
                    subject="Hi",
                    body="hello",
                    in_reply_to=orig_id,
                )

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.mail.create_draft_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_5xx"
    assert failed[0].payload["step"] == "patch_reply"
    assert failed[0].payload["draft_id"] == half_draft_id
    assert failed[0].payload["in_reply_to"] == orig_id


def test_create_draft_rejects_invalid_inputs(graph_mail_environment) -> None:
    sm = graph_mail_environment["sessionmaker"]
    created = graph_mail_environment["created_firm_ids"]

    firm_id, user_id = _seed(
        sm, slug=f"draft-input-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await create_draft(ctx, to=[], subject="x", body="x")
        with pytest.raises(ValueError):
            await create_draft(ctx, to=[""], subject="x", body="x")
        with pytest.raises(ValueError):
            await create_draft(
                ctx, to=["a@x"], subject="x", body="x", cc=[""]
            )
        with pytest.raises(ValueError):
            await create_draft(
                ctx, to=["a@x"], subject="x", body="x", in_reply_to=""
            )

    _run_with_ctx(sm, firm_id, user_id, body)
