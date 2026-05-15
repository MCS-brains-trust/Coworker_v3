"""Integration test for the ``approval_mark_delivery_failed``
orchestrator tool (pre-pilot Task 3).

Drives the tool through a single agent-loop iteration: a scripted
model caller returns a ``tool_use`` block targeting
``approval_mark_delivery_failed``; the engine validates input,
invokes the handler, and the handler updates the ``approval_items``
row by ``executed_internet_message_id``. The DB and trace
writes are real; Claude itself is mocked.
"""
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.approval.items import (
    CreateApprovalInput,
    approve,
    create_approval,
)
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.orchestrator.builtin_tools.approval import register as _register_approval
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.engine import (
    STATUS_COMPLETED,
    ModelCallResult,
    OrchestratorEngine,
)
from coworker.orchestrator.tools import ToolRegistry
from coworker.orchestrator.trace import AgentTraceWriter


class _ScriptedCaller:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def __call__(
        self, *, messages, system, tools, model,
        max_tokens, thinking_budget,
    ):
        self.calls.append({"system": system, "tool_count": len(tools)})
        return self._results.pop(0)


def _text_response(text_: str) -> ModelCallResult:
    return ModelCallResult(
        content=[{"type": "text", "text": text_}],
        stop_reason="end_turn",
        input_tokens=10, output_tokens=5,
        model="claude-sonnet-4-6",
    )


def _tool_use_response(
    tool_name: str, tool_input: dict, *, tool_use_id: str = "tu_1",
) -> ModelCallResult:
    return ModelCallResult(
        content=[
            {"type": "text", "text": "marking failed"},
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": tool_input,
            },
        ],
        stop_reason="tool_use",
        input_tokens=10, output_tokens=20,
        model="claude-sonnet-4-6",
    )


@pytest_asyncio.fixture
async def tool_env(test_database_url) -> AsyncIterator[dict]:
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
        "firms", "users", "audit_log", "agent_trace_steps",
        "agent_traces", "approval_items",
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
                "agent_trace_steps", "agent_traces",
                "approval_items", "audit_log", "users",
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


async def _seed_with_sent_item(
    sm, *, internet_message_id: str,
) -> tuple[uuid.UUID, Firm, uuid.UUID]:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id, name="Tool Firm",
            slug=f"t-{uuid.uuid4().hex[:8]}",
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Sender",
        )
        session.add_all([firm, user])
        await session.commit()
        row = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary="Draft",
                payload={
                    "from_user_id": str(user.id),
                    "to": ["client@example.com"],
                    "subject": "x", "body_html": "<p>x</p>",
                },
            ),
        )
        await session.commit()
        await approve(session, row.id, decided_by_user_id=user.id)
        await session.commit()
        row.status = "sent"
        row.delivery_status = "sent"
        row.executed_internet_message_id = internet_message_id
        await session.commit()
        item_id = row.id

        firm_obj = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        session.expunge(firm_obj)

    return firm_id, firm_obj, item_id


async def test_tool_flips_row_to_failed(tool_env) -> None:
    sm = tool_env["sm"]
    firm_id, firm, item_id = await _seed_with_sent_item(
        sm, internet_message_id="<original@graph.local>",
    )
    tool_env["created"].append(firm_id)

    caller = _ScriptedCaller(
        [
            _tool_use_response(
                "approval_mark_delivery_failed",
                {
                    "original_internet_message_id":
                        "<original@graph.local>",
                    "detail": "smtp; 550 5.1.1 user unknown",
                },
            ),
            _text_response("delivery marked failed"),
        ]
    )

    registry = ToolRegistry()
    _register_approval(registry)

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        writer = AgentTraceWriter(session, firm_id)
        trace_id = await writer.start_trace(goal="mark delivery failed")
        ctx = AgentContext(
            firm=attached, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=trace_id,
        )
        engine = OrchestratorEngine(model_caller=caller)
        result = await engine.run(
            ctx, goal="mark delivery failed",
            tools=registry, writer=writer,
        )
        await session.commit()

    assert result.status == STATUS_COMPLETED

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        assert row.delivery_status == "failed"
        assert row.delivery_status_detail == "smtp; 550 5.1.1 user unknown"


async def test_tool_uncorrelated_ndr_returns_false(tool_env) -> None:
    """Claude calls the tool with a Message-ID we don't know;
    the handler returns correlated=False (logged at WARN inside)
    and the row stays in delivery_status='sent'."""
    sm = tool_env["sm"]
    firm_id, firm, item_id = await _seed_with_sent_item(
        sm, internet_message_id="<original@graph.local>",
    )
    tool_env["created"].append(firm_id)

    caller = _ScriptedCaller(
        [
            _tool_use_response(
                "approval_mark_delivery_failed",
                {
                    "original_internet_message_id":
                        "<unknown@elsewhere>",
                    "detail": "no recipient",
                },
            ),
            _text_response("ndr could not be correlated"),
        ]
    )

    registry = ToolRegistry()
    _register_approval(registry)

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        writer = AgentTraceWriter(session, firm_id)
        trace_id = await writer.start_trace(goal="x")
        ctx = AgentContext(
            firm=attached, session=session,
            anthropic=None,  # type: ignore[arg-type]
            trace_id=trace_id,
        )
        engine = OrchestratorEngine(model_caller=caller)
        result = await engine.run(
            ctx, goal="x", tools=registry, writer=writer,
        )
        await session.commit()

    assert result.status == STATUS_COMPLETED

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == item_id)
            )
        ).scalar_one()
        # Row stays unchanged — uncorrelated NDR is a known case
        # the principal will see in WARN logs only.
        assert row.delivery_status == "sent"
