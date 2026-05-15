"""Integration tests for the sandbox firm primitive (pre-pilot Task 1).

Covers three behaviours:

- ``graph.mail.create_draft`` rewrites recipients to the firm's
  catchall when ``firm.is_sandbox`` is True, even with
  ``shadow_mode=False``.
- ``fusesign_client.create_envelope`` and ``send_reminder``
  honour sandbox mode.
- The dispatch sweep (Phase 9-4) ends up calling Graph with
  the rewritten recipients — confirms the rerouting is at the
  right layer.
- A non-sandbox firm is unaffected.
"""
import datetime as _dt
import json
import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.approval.dispatch import sweep_dispatch
from coworker.approval.items import (
    CreateApprovalInput,
    approve,
    create_approval,
)
from coworker.connectors.fusesign_client import (
    CreateEnvelopeDocument,
    CreateEnvelopeRecipient,
    FuseSignClient,
)
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.context import GraphContext
from coworker.graph.mail import create_draft
from coworker.security.encryption import encrypt_str

_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
_FUSESIGN_BASE_RE = re.compile(
    r"^https://api\.fusesign\.com/v[0-9]+/envelopes$"
)
_FUSESIGN_REMINDER_RE = re.compile(
    r"^https://api\.fusesign\.com/v[0-9]+/envelopes/[^/]+/reminders$"
)


@pytest_asyncio.fixture
async def sandbox_env(test_database_url) -> AsyncIterator[dict]:
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
    tables = (
        "firms", "users", "audit_log", "approval_items",
        "agent_traces", "agent_trace_steps",
    )
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in (
                "agent_trace_steps", "agent_traces", "approval_items",
                "audit_log", "users",
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
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


async def _seed_firm_and_user(
    sm,
    *,
    is_sandbox: bool,
    catchall: str | None = None,
    shadow_mode: bool = False,
) -> tuple[Firm, User]:
    firm_id = uuid.uuid4()
    firm_id_str = str(firm_id)
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id, name="Sandbox Firm",
            slug=f"sb-{uuid.uuid4().hex[:8]}",
            shadow_mode=shadow_mode,
            is_sandbox=is_sandbox,
            sandbox_outbound_catchall=catchall,
            fusesign_api_key_ciphertext=encrypt_str(
                "fs-api-key", firm_id=firm_id_str,
            ),
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Sender",
            ms_access_token_ciphertext=encrypt_str(
                "ms-tok", firm_id=firm_id_str,
            ),
            ms_token_expires_at=_dt.datetime.now(_dt.UTC)
            + _dt.timedelta(hours=1),
        )
        session.add_all([firm, user])
        await session.commit()
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        user = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        session.expunge_all()
        return firm, user


def _draft_response(*, msg_id: str = "draft-1") -> dict:
    return {
        "id": msg_id,
        "subject": "n/a",
        "from": None,
        "toRecipients": [],
        "ccRecipients": [],
        "bccRecipients": [],
        "body": {"contentType": "html", "content": ""},
        "bodyPreview": "",
        "receivedDateTime": "2026-05-15T11:00:00Z",
        "isRead": False,
        "hasAttachments": False,
    }


# ===========================================================================
# Schema-level CHECK constraint
# ===========================================================================


async def test_check_constraint_blocks_sandbox_without_catchall(
    sandbox_env,
) -> None:
    sm = sandbox_env["sm"]
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(
            id=firm_id, name="Bad Sandbox",
            slug=f"bad-{uuid.uuid4().hex[:8]}",
            is_sandbox=True,
            sandbox_outbound_catchall=None,  # constraint should reject
        ))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


# ===========================================================================
# graph.mail.create_draft rerouting
# ===========================================================================


async def test_sandbox_firm_create_draft_routes_to_catchall(
    sandbox_env,
) -> None:
    sm = sandbox_env["sm"]
    firm, user = await _seed_firm_and_user(
        sm, is_sandbox=True, catchall="sink@coworker.test",
    )
    sandbox_env["created"].append(firm.id)

    async with sm() as session, firm_context(firm.id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm, user=attached_user,
            access_token="tok", session=session,
        )
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_MESSAGES_URL).mock(
                return_value=httpx.Response(201, json=_draft_response()),
            )
            await create_draft(
                graph_ctx,
                to=["client-a@example.com", "client-b@example.com"],
                cc=["copy@example.com"],
                bcc=["bcc@example.com"],
                subject="Re: invoice",
                body="<p>Hi.</p>",
                body_content_type="html",
            )
            await session.commit()

    sent = json.loads(route.calls.last.request.read())
    to_addresses = [
        r["emailAddress"]["address"] for r in sent["toRecipients"]
    ]
    # All recipients collapsed to the single catchall; cc/bcc nulled
    # out entirely so no copies leak.
    assert to_addresses == ["sink@coworker.test"]
    assert "ccRecipients" not in sent
    assert "bccRecipients" not in sent
    assert sent["subject"] == (
        "[SANDBOX → client-a@example.com, client-b@example.com] Re: invoice"
    )


async def test_non_sandbox_firm_create_draft_unchanged(sandbox_env) -> None:
    sm = sandbox_env["sm"]
    firm, user = await _seed_firm_and_user(
        sm, is_sandbox=False, catchall=None,
    )
    sandbox_env["created"].append(firm.id)

    async with sm() as session, firm_context(firm.id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm, user=attached_user,
            access_token="tok", session=session,
        )
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_MESSAGES_URL).mock(
                return_value=httpx.Response(201, json=_draft_response()),
            )
            await create_draft(
                graph_ctx,
                to=["client@example.com"],
                subject="Re: invoice",
                body="<p>Hi.</p>",
                body_content_type="html",
            )
            await session.commit()

    sent = json.loads(route.calls.last.request.read())
    to_addresses = [
        r["emailAddress"]["address"] for r in sent["toRecipients"]
    ]
    assert to_addresses == ["client@example.com"]
    assert sent["subject"] == "Re: invoice"


# ===========================================================================
# Dispatch sweep end-to-end
# ===========================================================================


async def test_dispatch_sweep_sandbox_routes_through(sandbox_env) -> None:
    """The Phase 9-4 dispatch sweep terminates in create_draft. A
    sandbox firm whose approval item lists real-client recipients
    should still land its eventual Outlook draft on the catchall."""
    sm = sandbox_env["sm"]
    firm, user = await _seed_firm_and_user(
        sm, is_sandbox=True, catchall="sink@coworker.test",
        shadow_mode=False,
    )
    sandbox_env["created"].append(firm.id)

    async with sm() as session, firm_context(firm.id):
        item = await create_approval(
            session, firm.id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary="Reply to real client",
                payload={
                    "from_user_id": str(user.id),
                    "to": ["real-client@example.com"],
                    "subject": "Re: your query",
                    "body_html": "<p>hi</p>",
                },
            ),
        )
        await session.commit()
        await approve(session, item.id, decided_by_user_id=user.id)
        await session.commit()
        item_id = item.id

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.post(_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=_draft_response()),
        )
        result = await sweep_dispatch(
            sessionmaker=sm, firm_ids=[firm.id],
        )

    assert result.dispatched == 1
    sent = json.loads(route.calls.last.request.read())
    to_addresses = [
        r["emailAddress"]["address"] for r in sent["toRecipients"]
    ]
    assert to_addresses == ["sink@coworker.test"]
    assert sent["subject"].startswith("[SANDBOX → real-client@example.com]")

    async with sm() as session, firm_context(firm.id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "sent"


# ===========================================================================
# FuseSign rerouting
# ===========================================================================


async def test_fusesign_create_envelope_sandbox_rewrites_emails(
    sandbox_env,
) -> None:
    sm = sandbox_env["sm"]
    firm, _user = await _seed_firm_and_user(
        sm, is_sandbox=True, catchall="sink@coworker.test",
    )
    sandbox_env["created"].append(firm.id)

    async with sm() as session, firm_context(firm.id):
        attached_firm = await session.merge(firm)
        client = FuseSignClient(
            session=session, firm=attached_firm,
            actor_type="user", actor_id=str(uuid.uuid4()),
        )
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(url__regex=_FUSESIGN_BASE_RE).mock(
                return_value=httpx.Response(
                    201,
                    json={
                        "data": {
                            "id": "env-1",
                            "name": "Engagement",
                            "status": "sent",
                            "created_at": "2026-05-15T11:00:00Z",
                            "updated_at": "2026-05-15T11:00:00Z",
                            "recipients": [],
                            "documents": [],
                        }
                    },
                ),
            )
            await client.create_envelope(
                name="Engagement letter",
                recipients=[
                    CreateEnvelopeRecipient(
                        name="Jane Doe", email="jane@acme.example",
                    ),
                    CreateEnvelopeRecipient(
                        name="Bob Doe", email="bob@acme.example",
                    ),
                ],
                documents=[
                    CreateEnvelopeDocument(
                        name="letter.pdf", content_base64="ZG9j",
                    ),
                ],
            )
            await session.commit()

    sent = json.loads(route.calls.last.request.read())
    # Names preserved; emails all rewritten to the catchall.
    sent_emails = {r["email"] for r in sent["recipients"]}
    assert sent_emails == {"sink@coworker.test"}
    sent_names = {r["name"] for r in sent["recipients"]}
    assert sent_names == {"Jane Doe", "Bob Doe"}


async def test_fusesign_send_reminder_sandbox_noop(sandbox_env) -> None:
    """Reminder is a no-op (no FuseSign HTTP call) when the firm is
    in sandbox mode — the original envelope's recipient is already
    the catchall, so another reminder adds no signal."""
    sm = sandbox_env["sm"]
    firm, _user = await _seed_firm_and_user(
        sm, is_sandbox=True, catchall="sink@coworker.test",
    )
    sandbox_env["created"].append(firm.id)

    async with sm() as session, firm_context(firm.id):
        attached_firm = await session.merge(firm)
        client = FuseSignClient(
            session=session, firm=attached_firm,
            actor_type="user", actor_id=str(uuid.uuid4()),
        )
        with respx.mock(assert_all_called=False) as rmock:
            # No mock registered — if the client tried the HTTP call
            # respx would fail. The no-op path is the assertion.
            await client.send_reminder("env-1")
            assert rmock.calls.call_count == 0
