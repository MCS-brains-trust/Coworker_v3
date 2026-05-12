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
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.context import GraphContext
from coworker.graph.mail import (
    EmailAttachment,
    FullEmailMessage,
    InboxAddress,
    InboxMessage,
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


def _seed(sessionmaker, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a minimal firm + user. Returns (firm_id, user_id)."""

    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Mail Test Firm",
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
