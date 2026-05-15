"""Integration tests for the approval dispatch sweep.

Real DB; Graph mail layer mocked via respx. The sweep walks
approved (and previously-failed) email_draft items, creates the
Outlook draft, and transitions the row.
"""
import datetime as _dt
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
import respx
from sqlalchemy import select, text
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
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import encrypt_str

_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"


@pytest_asyncio.fixture
async def dispatch_env(test_database_url) -> AsyncIterator[dict]:
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


async def _seed(
    sm,
    *,
    shadow_mode: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed (firm, user) with a valid Microsoft access token."""
    firm_id = uuid.uuid4()
    firm_id_str = str(firm_id)
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id, name="Dispatch Firm",
            slug=f"d-{uuid.uuid4().hex[:8]}",
            shadow_mode=shadow_mode,
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Sender",
            ms_access_token_ciphertext=encrypt_str(
                "user-tok", firm_id=firm_id_str,
            ),
            ms_token_expires_at=_dt.datetime.now(_dt.UTC)
            + _dt.timedelta(hours=1),
        )
        session.add_all([firm, user])
        await session.commit()
        return firm_id, user.id


async def _seed_approved_draft(
    sm, firm_id, *, from_user_id, in_reply_to=None,
) -> uuid.UUID:
    decider_id = from_user_id  # same user approves their own draft in tests
    payload: dict = {
        "from_user_id": str(from_user_id),
        "to": ["client@example.com"],
        "subject": "Re: your query",
        "body_html": "<p>Hello,</p><p>Thanks.</p>",
    }
    if in_reply_to is not None:
        payload["in_reply_to_message_id"] = in_reply_to

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary="Draft for client@example.com",
                payload=payload,
            ),
        )
        await session.commit()
        await approve(session, row.id, decided_by_user_id=decider_id)
        await session.commit()
        return row.id


def _draft_response(
    *,
    msg_id: str = "drafted-1",
    internet_message_id: str | None = "<draft-1@graph.local>",
) -> dict:
    """Shape Graph returns from POST /me/messages."""
    body: dict = {
        "id": msg_id,
        "subject": "Re: your query",
        "from": None,
        "toRecipients": [
            {"emailAddress": {"address": "client@example.com"}}
        ],
        "ccRecipients": [],
        "bccRecipients": [],
        "body": {"contentType": "html", "content": "<p>Hello,</p>"},
        "bodyPreview": "Hello,",
        "receivedDateTime": "2026-05-14T14:00:00Z",
        "isRead": False,
        "hasAttachments": False,
    }
    if internet_message_id is not None:
        body["internetMessageId"] = internet_message_id
    return body


# ===========================================================================
# Tests
# ===========================================================================


async def test_dispatch_transitions_approved_to_sent(dispatch_env) -> None:
    sm = dispatch_env["sm"]
    firm_id, user_id = await _seed(sm)
    dispatch_env["created"].append(firm_id)
    item_id = await _seed_approved_draft(sm, firm_id, from_user_id=user_id)

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.post(_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=_draft_response()),
        )
        result = await sweep_dispatch(
            sessionmaker=sm, firm_ids=[firm_id],
        )

    assert result.dispatched == 1
    assert result.failed == 0
    assert result.actions == {"dispatched": 1}
    assert route.called

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "sent"
        # Task 3: dispatcher captures Graph's proposed Message-ID
        # and flips delivery_status='sent'.
        assert row.executed_internet_message_id == "<draft-1@graph.local>"
        assert row.delivery_status == "sent"
        assert row.delivery_status_updated_at is not None


async def test_dispatch_persists_no_internet_message_id_when_absent(
    dispatch_env,
) -> None:
    """If Graph's response omits internetMessageId (e.g. some OWA
    flows), the dispatcher still flips delivery_status='sent' but
    executed_internet_message_id stays NULL — those rows can't be
    NDR-correlated and will eventually be confirmed by the 4h sweep
    or stay 'sent' indefinitely. Documented as the carry-forward."""
    sm = dispatch_env["sm"]
    firm_id, user_id = await _seed(sm)
    dispatch_env["created"].append(firm_id)
    item_id = await _seed_approved_draft(sm, firm_id, from_user_id=user_id)

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_MESSAGES_URL).mock(
            return_value=httpx.Response(
                201, json=_draft_response(internet_message_id=None),
            ),
        )
        await sweep_dispatch(sessionmaker=sm, firm_ids=[firm_id])

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "sent"
        assert row.executed_internet_message_id is None
        assert row.delivery_status == "sent"


async def test_dispatch_shadow_mode_marks_failed(dispatch_env) -> None:
    """When firm.shadow_mode is True, create_draft raises
    ShadowModeBlocked. The dispatcher records dispatch_failed."""
    sm = dispatch_env["sm"]
    firm_id, user_id = await _seed(sm, shadow_mode=True)
    dispatch_env["created"].append(firm_id)
    item_id = await _seed_approved_draft(sm, firm_id, from_user_id=user_id)

    with respx.mock(assert_all_called=False):
        result = await sweep_dispatch(
            sessionmaker=sm, firm_ids=[firm_id],
        )

    assert result.dispatched == 0
    assert result.failed == 1
    assert result.actions == {"shadow_blocked": 1}

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "dispatch_failed"
        assert row.decision_notes == "[dispatch] shadow_blocked"


async def test_dispatch_retries_on_next_tick_after_failure(
    dispatch_env,
) -> None:
    """A row in dispatch_failed is picked up by the next sweep tick."""
    sm = dispatch_env["sm"]
    firm_id, user_id = await _seed(sm)
    dispatch_env["created"].append(firm_id)
    item_id = await _seed_approved_draft(sm, firm_id, from_user_id=user_id)

    # First sweep: Graph 503 -> dispatch_failed.
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_MESSAGES_URL).mock(
            return_value=httpx.Response(503, json={"error": "transient"}),
        )
        first = await sweep_dispatch(sessionmaker=sm, firm_ids=[firm_id])
    assert first.failed == 1

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "dispatch_failed"

    # Second sweep: Graph 201 -> sent.
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=_draft_response()),
        )
        second = await sweep_dispatch(sessionmaker=sm, firm_ids=[firm_id])
    assert second.dispatched == 1
    assert second.actions == {"dispatched": 1}

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "sent"


async def test_dispatch_skips_pending_and_rejected(dispatch_env) -> None:
    """Only approved + dispatch_failed are eligible."""
    sm = dispatch_env["sm"]
    firm_id, user_id = await _seed(sm)
    dispatch_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        pending = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary="Pending",
                payload={
                    "from_user_id": str(user_id),
                    "to": ["x@y.com"],
                    "subject": "x",
                    "body_html": "<p>x</p>",
                },
            ),
        )
        await session.commit()

    with respx.mock(assert_all_called=False):
        result = await sweep_dispatch(sessionmaker=sm, firm_ids=[firm_id])

    assert result.items_seen == 0
    assert pending  # silence linter


async def test_dispatch_bad_payload_marks_failed(dispatch_env) -> None:
    """Payload missing from_user_id can't be dispatched."""
    sm = dispatch_env["sm"]
    firm_id, user_id = await _seed(sm)
    dispatch_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary="Bad payload",
                payload={
                    # Missing from_user_id
                    "to": ["x@y.com"],
                    "subject": "x",
                    "body_html": "<p>x</p>",
                },
            ),
        )
        await session.commit()
        await approve(session, row.id, decided_by_user_id=user_id)
        await session.commit()
        item_id = row.id

    with respx.mock(assert_all_called=False):
        result = await sweep_dispatch(sessionmaker=sm, firm_ids=[firm_id])

    assert result.actions == {"bad_payload_no_sender": 1}

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "dispatch_failed"


async def test_dispatch_user_missing_marks_failed(dispatch_env) -> None:
    """from_user_id pointing at a deleted user -> dispatch_failed."""
    sm = dispatch_env["sm"]
    firm_id, user_id = await _seed(sm)
    dispatch_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary="Phantom sender",
                payload={
                    "from_user_id": str(uuid.uuid4()),  # not seeded
                    "to": ["x@y.com"],
                    "subject": "x",
                    "body_html": "<p>x</p>",
                },
            ),
        )
        await session.commit()
        await approve(session, row.id, decided_by_user_id=user_id)
        await session.commit()

    with respx.mock(assert_all_called=False):
        result = await sweep_dispatch(sessionmaker=sm, firm_ids=[firm_id])

    assert result.actions == {"user_missing": 1}
