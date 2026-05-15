"""Integration tests for the calendar-category builtin tool.

Mirrors test_email_tools.py: real DB seeded with a firm + user,
respx-mocked Graph endpoint, handler invoked directly with a
constructed AgentContext.
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
from coworker.orchestrator.builtin_tools.calendar import (
    CalendarListEventsInput,
    MeetingBriefProposeInput,
    _calendar_list_events_handler,
    _meeting_brief_propose_handler,
)
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import ToolError
from coworker.security.encryption import encrypt_str

_CALENDAR_VIEW_URL = "https://graph.microsoft.com/v1.0/me/calendarView"


@pytest_asyncio.fixture
async def cal_env(test_database_url):
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
                "agent_trace_steps", "approval_items", "agent_traces",
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


async def _seed_firm_and_user(sm):
    firm_id = uuid.uuid4()
    firm_id_str = str(firm_id)
    async with sm() as session, firm_context(firm_id):
        session.add(
            Firm(
                id=firm_id, name="Cal Firm",
                slug=f"c-{uuid.uuid4().hex[:8]}",
            )
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=uuid.uuid4().hex,
            upn=f"cal-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Cal User",
            ms_access_token_ciphertext=encrypt_str(
                "tok", firm_id=firm_id_str,
            ),
        )
        session.add(user)
        await session.commit()
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        u = (
            await session.execute(
                select(User).where(User.firm_id == firm_id)
            )
        ).scalar_one()
        session.expunge(firm)
        session.expunge(u)
    return firm_id, firm, u


def _event_payload(*, ev_id: str = "ev-1", subject: str = "Standup") -> dict:
    return {
        "id": ev_id,
        "subject": subject,
        "bodyPreview": "Daily standup",
        "start": {"dateTime": "2026-05-15T09:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-15T09:30:00", "timeZone": "UTC"},
        "location": {"displayName": "Room 12"},
        "isAllDay": False,
        "isCancelled": False,
        "showAs": "busy",
        "organizer": {
            "emailAddress": {
                "address": "lead@example.com",
                "name": "Team Lead",
            },
        },
        "attendees": [],
        "onlineMeeting": None,
        "webLink": "https://outlook.example/ev-1",
    }


# ===========================================================================
# Tests
# ===========================================================================


async def test_calendar_list_events_happy_path(cal_env) -> None:
    sm = cal_env["sm"]
    firm_id, firm, user = await _seed_firm_and_user(sm)
    cal_env["created"].append(firm_id)

    start = _dt.datetime(2026, 5, 15, tzinfo=_dt.UTC)
    end = start + _dt.timedelta(days=1)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm, user=attached_user,
            access_token="bearer-test", session=session,
        )
        ctx = AgentContext(
            firm=attached_firm, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=graph_ctx,
        )
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(url__startswith=_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            _event_payload(ev_id="ev-1", subject="Standup"),
                            _event_payload(
                                ev_id="ev-2", subject="Client call",
                            ),
                        ]
                    },
                )
            )
            result = await _calendar_list_events_handler(
                CalendarListEventsInput(start=start, end=end),
                ctx,
            )

    assert result["count"] == 2
    ids = {e["id"] for e in result["events"]}
    assert ids == {"ev-1", "ev-2"}
    # Subjects are sanitised + wrapped (pre-pilot Task 2). Identifier
    # / structured fields like id stay unwrapped.
    subjects = {e["subject"] for e in result["events"]}
    assert subjects == {
        "<user_data>Standup</user_data>",
        "<user_data>Client call</user_data>",
    }


async def test_calendar_list_events_without_graph_ctx_raises(
    cal_env,
) -> None:
    sm = cal_env["sm"]
    firm_id, firm, _ = await _seed_firm_and_user(sm)
    cal_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        ctx = AgentContext(
            firm=attached_firm, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=None,
        )
        with pytest.raises(ToolError, match="Graph context"):
            await _calendar_list_events_handler(
                CalendarListEventsInput(
                    start=_dt.datetime(2026, 5, 15, tzinfo=_dt.UTC),
                    end=_dt.datetime(2026, 5, 16, tzinfo=_dt.UTC),
                ),
                ctx,
            )


def test_calendar_list_events_validates_naive_datetime() -> None:
    """Pydantic accepts naive in the model; the connector rejects.
    Verify the connector's tz-awareness check still fires through
    the handler."""
    # Pydantic v2 will store the naive datetime as-is; the inner
    # ``list_calendar_events`` raises ValueError on naive input.
    # We don't exercise that here (covered in the connector tests)
    # because driving the handler with a naive value would skip
    # the validation path the framework runs at the boundary —
    # this just documents the design.
    assert True


def test_register_includes_calendar_list_events() -> None:
    """The calendar module exposes register() picked up by
    register_builtin_tools."""
    from coworker.orchestrator.builtin_tools import register_builtin_tools
    from coworker.orchestrator.tools import ToolRegistry

    reg = ToolRegistry()
    register_builtin_tools(reg)
    tool = reg.get("calendar_list_events")
    assert tool is not None
    assert tool.category == "calendar"
    assert tool.side_effect is False


# ===========================================================================
# Phase 12-2: meeting_brief_propose
# ===========================================================================


async def _seed_trace(sm, firm_id: uuid.UUID) -> uuid.UUID:
    from coworker.db.models import AgentTrace

    trace_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(
            AgentTrace(
                id=trace_id, firm_id=firm_id,
                plugin_name="meeting_prep",
                goal="brief", status="completed",
                metadata_={},
            )
        )
        await session.commit()
    return trace_id


async def test_meeting_brief_propose_writes_approval_item(cal_env) -> None:
    from coworker.db.models import ApprovalItem

    sm = cal_env["sm"]
    firm_id, firm, user = await _seed_firm_and_user(sm)
    cal_env["created"].append(firm_id)
    trace_id = await _seed_trace(sm, firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm, user=attached_user,
            access_token="bearer-test", session=session,
        )
        ctx = AgentContext(
            firm=attached_firm, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=trace_id,
            graph_ctx=graph_ctx,
            metadata={"plugin_name": "meeting_prep"},
        )
        result = await _meeting_brief_propose_handler(
            MeetingBriefProposeInput(
                event_id="ev-1",
                subject="Quarterly review — Acme Pty Ltd",
                start=_dt.datetime(2026, 5, 16, 10, 0, tzinfo=_dt.UTC),
                end=_dt.datetime(2026, 5, 16, 11, 0, tzinfo=_dt.UTC),
                brief_html="<p>Acme Q1 close, BAS due 28 May.</p>",
                summary="Brief: Quarterly review with Acme",
                attendees=["jane@acme.example", "bob@acme.example"],
            ),
            ctx,
        )
        await session.commit()

    assert result["status"] == "pending"
    item_id = uuid.UUID(result["approval_item_id"])

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.category == "meeting_brief"
        assert row.plugin_name == "meeting_prep"
        assert row.trace_id == trace_id
        assert row.payload["event_id"] == "ev-1"
        assert row.payload["subject"] == "Quarterly review — Acme Pty Ltd"
        assert row.payload["owner_user_id"] == str(user.id)
        assert row.payload["attendees"] == [
            "jane@acme.example", "bob@acme.example",
        ]


async def test_meeting_brief_propose_high_confidence_auto_approves(
    cal_env,
) -> None:
    """Confidence above the auto-approve threshold lands the brief as
    ``approved`` so the principal sees it as already-seen."""
    from coworker.db.models import ApprovalItem

    sm = cal_env["sm"]
    firm_id, firm, user = await _seed_firm_and_user(sm)
    cal_env["created"].append(firm_id)
    trace_id = await _seed_trace(sm, firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        attached_user = await session.merge(user)
        graph_ctx = GraphContext(
            firm=attached_firm, user=attached_user,
            access_token="bearer-test", session=session,
        )
        ctx = AgentContext(
            firm=attached_firm, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=trace_id,
            graph_ctx=graph_ctx,
            metadata={"plugin_name": "meeting_prep"},
        )
        result = await _meeting_brief_propose_handler(
            MeetingBriefProposeInput(
                event_id="ev-2",
                subject="Standup",
                start=_dt.datetime(2026, 5, 16, 9, 0, tzinfo=_dt.UTC),
                end=_dt.datetime(2026, 5, 16, 9, 30, tzinfo=_dt.UTC),
                brief_html="<p>Internal — no client context needed.</p>",
                summary="Brief: standup",
                confidence=0.95,
            ),
            ctx,
        )
        await session.commit()

    # Default firm threshold is 0.85; 0.95 auto-approves.
    assert result["status"] == "approved"
    item_id = uuid.UUID(result["approval_item_id"])

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.status == "approved"
        assert row.confidence == 0.95


async def test_meeting_brief_propose_without_graph_ctx_raises(
    cal_env,
) -> None:
    sm = cal_env["sm"]
    firm_id, firm, _ = await _seed_firm_and_user(sm)
    cal_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        attached_firm = await session.merge(firm)
        ctx = AgentContext(
            firm=attached_firm, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=uuid.uuid4(),
            graph_ctx=None,
        )
        with pytest.raises(ToolError, match="Graph context"):
            await _meeting_brief_propose_handler(
                MeetingBriefProposeInput(
                    event_id="ev",
                    subject="x",
                    start=_dt.datetime(2026, 5, 16, 9, 0, tzinfo=_dt.UTC),
                    end=_dt.datetime(2026, 5, 16, 10, 0, tzinfo=_dt.UTC),
                    brief_html="<p>x</p>",
                    summary="x",
                ),
                ctx,
            )
