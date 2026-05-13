"""Integration tests for the email-category builtin tools.

Drives each handler directly with a constructed AgentContext +
respx-mocked Graph HTTP. End-to-end coverage via an agent loop
comes in test_smart_responder_e2e (or a similar suite); this
file focuses on each handler's per-call contract.
"""
import datetime as _dt
import uuid

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.context import GraphContext
from coworker.orchestrator.builtin_tools.email import (
    EmailCreateDraftInput,
    EmailGetMessageInput,
    EmailMarkAsReadInput,
    _email_create_draft_handler,
    _email_get_message_handler,
    _email_mark_as_read_handler,
)
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import ToolError
from coworker.security.encryption import encrypt_str

_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"


# ---------------------------------------------------------------------------
# Fixtures (compact version — only the firm+user shape we need)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def email_env(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = ("firms", "users", "audit_log")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
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


async def _seed_firm_and_user(sm, *, shadow_mode: bool = False):
    firm_id = uuid.uuid4()
    firm_id_str = str(firm_id)
    async with sm() as session, firm_context(firm_id):
        session.add(
            Firm(
                id=firm_id,
                name="Email Tools Firm",
                slug=f"e-{uuid.uuid4().hex[:8]}",
                shadow_mode=shadow_mode,
                azure_tenant_id=str(uuid.uuid4()),
                azure_client_id=str(uuid.uuid4()),
                azure_client_secret_ciphertext=encrypt_str(
                    "secret", firm_id=firm_id_str
                ),
            )
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=uuid.uuid4().hex,
            upn=f"email-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Email Test User",
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
        await session.commit()
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        u = (
            await session.execute(select(User).where(User.firm_id == firm_id))
        ).scalar_one()
        session.expunge(firm)
        session.expunge(u)
    return firm_id, firm, u


async def _build_ctx(
    sm,
    firm,
    user,
) -> tuple[AsyncSession, AgentContext]:
    """Open a session inside firm_context and build AgentContext + GraphContext."""
    # Caller wraps with `async with sm() as session, firm_context(firm.id):`
    raise NotImplementedError  # see inline use below


def _full_message_payload() -> dict:
    return {
        "id": "msg-1",
        "subject": "Test subject",
        "receivedDateTime": "2026-05-10T10:00:00Z",
        "body": {"contentType": "html", "content": "<p>Hello</p>"},
        "isRead": False,
        "hasAttachments": False,
        "conversationId": "conv-1",
        "from": {
            "emailAddress": {"address": "alice@x.com", "name": "Alice"}
        },
        "toRecipients": [
            {"emailAddress": {"address": "bob@x.com", "name": "Bob"}}
        ],
        "ccRecipients": [],
        "bccRecipients": [],
    }


def _draft_response() -> dict:
    return {
        "id": "draft-new-1",
        "subject": "Re: Test subject",
        "receivedDateTime": "2026-05-10T11:00:00Z",
        "body": {"contentType": "html", "content": "<p>draft</p>"},
        "isRead": True,
        "hasAttachments": False,
        "conversationId": "conv-1",
        "toRecipients": [
            {"emailAddress": {"address": "alice@x.com"}}
        ],
        "ccRecipients": [],
        "bccRecipients": [],
        "from": {"emailAddress": {"address": "me@x.com"}},
    }


# ===========================================================================
# email_get_message
# ===========================================================================


async def test_email_get_message_happy_path(email_env) -> None:
    sm = email_env["sm"]
    firm_id, firm, user = await _seed_firm_and_user(sm)
    email_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm,
            user=attached_user,
            access_token="bearer-test",
            session=session,
        )
        ctx = AgentContext(
            firm=attached_firm, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=graph_ctx,
        )
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_MESSAGES_URL}/msg-1").mock(
                return_value=httpx.Response(200, json=_full_message_payload())
            )
            result = await _email_get_message_handler(
                EmailGetMessageInput(message_id="msg-1"), ctx
            )

    assert result["id"] == "msg-1"
    assert result["sender"]["email"] == "alice@x.com"
    assert result["body"]["content_type"] == "html"
    assert result["is_read"] is False


async def test_email_get_message_without_graph_ctx_raises_tool_error(
    email_env,
) -> None:
    sm = email_env["sm"]
    firm_id, firm, _ = await _seed_firm_and_user(sm)
    email_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        ctx = AgentContext(
            firm=attached_firm,
            session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=None,
        )
        with pytest.raises(ToolError, match="Graph context"):
            await _email_get_message_handler(
                EmailGetMessageInput(message_id="msg-1"), ctx
            )


# ===========================================================================
# email_create_draft
# ===========================================================================


async def test_email_create_draft_happy_path_outside_shadow_mode(
    email_env,
) -> None:
    sm = email_env["sm"]
    firm_id, firm, user = await _seed_firm_and_user(sm, shadow_mode=False)
    email_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm,
            user=attached_user,
            access_token="bearer-test",
            session=session,
        )
        ctx = AgentContext(
            firm=attached_firm,
            session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=graph_ctx,
        )
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(201, json=_draft_response())
            )
            result = await _email_create_draft_handler(
                EmailCreateDraftInput(
                    to=["alice@x.com"],
                    subject="Re: Test",
                    body="Hi Alice",
                ),
                ctx,
            )
        await session.commit()

    assert result["draft_id"] == "draft-new-1"
    assert result["to_recipients"][0]["email"] == "alice@x.com"


async def test_email_create_draft_without_graph_ctx_raises_tool_error(
    email_env,
) -> None:
    sm = email_env["sm"]
    firm_id, firm, _ = await _seed_firm_and_user(sm)
    email_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        ctx = AgentContext(
            firm=attached_firm,
            session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=None,
        )
        with pytest.raises(ToolError, match="Graph context"):
            await _email_create_draft_handler(
                EmailCreateDraftInput(
                    to=["x@x"], subject="x", body="x",
                ),
                ctx,
            )


# ===========================================================================
# email_mark_as_read
# ===========================================================================


async def test_email_mark_as_read_happy_path(email_env) -> None:
    sm = email_env["sm"]
    firm_id, firm, user = await _seed_firm_and_user(sm, shadow_mode=False)
    email_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm,
            user=attached_user,
            access_token="bearer-test",
            session=session,
        )
        ctx = AgentContext(
            firm=attached_firm,
            session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=graph_ctx,
        )
        with respx.mock(assert_all_called=True) as rmock:
            rmock.patch(f"{_GRAPH_MESSAGES_URL}/msg-1").mock(
                return_value=httpx.Response(200, json={"id": "msg-1"})
            )
            result = await _email_mark_as_read_handler(
                EmailMarkAsReadInput(message_id="msg-1"), ctx
            )
        await session.commit()

    assert result["message_id"] == "msg-1"
    assert result["is_read"] is True


async def test_email_mark_as_read_without_graph_ctx_raises_tool_error(
    email_env,
) -> None:
    sm = email_env["sm"]
    firm_id, firm, _ = await _seed_firm_and_user(sm)
    email_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        ctx = AgentContext(
            firm=attached_firm,
            session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=None,
        )
        with pytest.raises(ToolError, match="Graph context"):
            await _email_mark_as_read_handler(
                EmailMarkAsReadInput(message_id="msg-1"), ctx,
            )


# ===========================================================================
# Registry assembly
# ===========================================================================


def test_register_builtin_tools_includes_email_tools() -> None:
    from coworker.orchestrator.builtin_tools import register_builtin_tools
    from coworker.orchestrator.tools import ToolRegistry

    reg = ToolRegistry()
    register_builtin_tools(reg)
    names = {t.name for t in reg.all()}
    assert "email_get_message" in names
    assert "email_create_draft" in names
    assert "email_mark_as_read" in names

    # email_create_draft and email_mark_as_read are side-effect tools.
    assert reg.get("email_create_draft").side_effect is True
    assert reg.get("email_mark_as_read").side_effect is True
    # email_get_message is read-only.
    assert reg.get("email_get_message").side_effect is False
