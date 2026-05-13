"""Integration tests for ``AgentTraceWriter``.

Real Postgres test DB so the RLS + UNIQUE-constraint guarantees
on agent_traces / agent_trace_steps actually run.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import AgentTrace, AgentTraceStep, Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.orchestrator.trace import (
    AgentTraceWriter,
    TraceNotStartedError,
)


@pytest_asyncio.fixture
async def trace_env(test_database_url):
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
        "firms", "users", "audit_log", "token_usage",
        "client_interactions", "lessons", "documents",
        "entity_relationships", "entities", "jobs", "deadlines",
        "agent_trace_steps", "agent_traces",
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
                "entity_relationships", "deadlines", "jobs",
                "documents", "lessons", "client_interactions",
                "entities", "audit_log", "token_usage", "users",
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


async def _seed_firm(sm) -> uuid.UUID:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(
            Firm(id=firm_id, name="Trace Firm", slug=f"t-{uuid.uuid4().hex[:8]}")
        )
        await session.commit()
    return firm_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_start_trace_then_record_steps_round_trip(trace_env) -> None:
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        writer = AgentTraceWriter(session, firm_id)
        trace_id = await writer.start_trace(
            goal="Draft a reply to Alice",
            plugin_name="smart_responder",
            metadata={"firm_role": "principal"},
        )
        await writer.record_model_call(
            model="claude-sonnet-4-6",
            request_messages=[{"role": "user", "content": "hello"}],
            response_content=[
                {"type": "text", "text": "Hi! I'll help with that."}
            ],
            input_tokens=100,
            output_tokens=20,
            cost_cents=2,
            duration_ms=500,
            stop_reason="end_turn",
        )
        await writer.record_tool_call(
            tool_name="memory_query",
            tool_use_id="toolu_1",
            input_data={"query": "Alice"},
        )
        await writer.record_tool_result(
            tool_name="memory_query",
            tool_use_id="toolu_1",
            result={"hits": 3},
            is_error=False,
            duration_ms=80,
            cost_cents=1,
        )
        await writer.finish_trace(status="completed")
        await session.commit()

    # Re-read and assert.
    async with sm() as session, firm_context(firm_id):
        trace = (
            await session.execute(
                select(AgentTrace).where(AgentTrace.id == trace_id)
            )
        ).scalar_one()
        assert trace.goal == "Draft a reply to Alice"
        assert trace.plugin_name == "smart_responder"
        assert trace.status == "completed"
        assert trace.ended_at is not None
        # 100 + 0 + 0
        assert trace.total_input_tokens == 100
        # 20 + 0 + 0
        assert trace.total_output_tokens == 20
        # 2 + 0 + 1
        assert trace.total_cost_cents == 3
        assert trace.num_steps == 3

        steps = (
            await session.execute(
                select(AgentTraceStep)
                .where(AgentTraceStep.trace_id == trace_id)
                .order_by(AgentTraceStep.step_index)
            )
        ).scalars().all()
        assert [s.step_index for s in steps] == [0, 1, 2]
        assert [s.step_type for s in steps] == [
            "model_call",
            "tool_call",
            "tool_result",
        ]
        # Content is the source of truth for replay.
        assert "I'll help" in steps[0].content["response_content"][0]["text"]
        assert steps[1].content["input"]["query"] == "Alice"
        assert steps[2].content["result"]["hits"] == 3
        assert steps[2].is_error is False


async def test_record_before_start_raises(trace_env) -> None:
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        writer = AgentTraceWriter(session, firm_id)
        with pytest.raises(TraceNotStartedError):
            await writer.record_error(message="oops")
        with pytest.raises(TraceNotStartedError):
            _ = writer.trace_id


async def test_step_index_is_monotonic_and_starts_at_zero(trace_env) -> None:
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        writer = AgentTraceWriter(session, firm_id)
        trace_id = await writer.start_trace(goal="x")
        for i in range(5):
            await writer.record_tool_call(
                tool_name="t",
                tool_use_id=f"u-{i}",
                input_data={"i": i},
            )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        steps = (
            await session.execute(
                select(AgentTraceStep)
                .where(AgentTraceStep.trace_id == trace_id)
                .order_by(AgentTraceStep.step_index)
            )
        ).scalars().all()
        assert [s.step_index for s in steps] == [0, 1, 2, 3, 4]


async def test_tool_result_with_error_records_error_class(trace_env) -> None:
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        writer = AgentTraceWriter(session, firm_id)
        trace_id = await writer.start_trace(goal="x")
        await writer.record_tool_result(
            tool_name="memory_query",
            tool_use_id="u-1",
            result={"message": "client not found"},
            is_error=True,
            duration_ms=10,
            error_class="ToolError",
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        step = (
            await session.execute(
                select(AgentTraceStep).where(
                    AgentTraceStep.trace_id == trace_id
                )
            )
        ).scalar_one()
        assert step.is_error is True
        assert step.content["error_class"] == "ToolError"


async def test_record_error_inserts_an_error_step(trace_env) -> None:
    """Engine-level errors (caught from a tool's unrelated exception)
    land as a step the model never sees.
    """
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        writer = AgentTraceWriter(session, firm_id)
        await writer.start_trace(goal="x")
        await writer.record_error(
            message="handler raised TypeError",
            error_class="TypeError",
            metadata={"handler": "memory_query"},
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        steps = (
            await session.execute(select(AgentTraceStep))
        ).scalars().all()
        assert len(steps) == 1
        assert steps[0].step_type == "error"
        assert steps[0].is_error is True
        assert steps[0].content["error_class"] == "TypeError"


async def test_finish_trace_is_idempotent(trace_env) -> None:
    """Calling finish_trace twice (e.g. from a wrapper's finally + the
    engine's explicit call) doesn't double-update.
    """
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        writer = AgentTraceWriter(session, firm_id)
        trace_id = await writer.start_trace(goal="x")
        await writer.finish_trace(status="completed")
        # Second call is a no-op because status != 'running'.
        await writer.finish_trace(status="failed")
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        trace = (
            await session.execute(
                select(AgentTrace).where(AgentTrace.id == trace_id)
            )
        ).scalar_one()
        assert trace.status == "completed"


async def test_totals_accumulate_across_many_model_calls(trace_env) -> None:
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        writer = AgentTraceWriter(session, firm_id)
        trace_id = await writer.start_trace(goal="x")
        for _ in range(3):
            await writer.record_model_call(
                model="claude-sonnet-4-6",
                request_messages=[],
                response_content=[],
                input_tokens=10,
                output_tokens=5,
                cost_cents=1,
                duration_ms=100,
            )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        trace = (
            await session.execute(
                select(AgentTrace).where(AgentTrace.id == trace_id)
            )
        ).scalar_one()
        assert trace.total_input_tokens == 30
        assert trace.total_output_tokens == 15
        assert trace.total_cost_cents == 3
        assert trace.num_steps == 3


async def test_parent_trace_id_creates_a_sub_trace(trace_env) -> None:
    sm = trace_env["sm"]
    firm_id = await _seed_firm(sm)
    trace_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        parent = AgentTraceWriter(session, firm_id)
        parent_id = await parent.start_trace(goal="parent")

        sub = AgentTraceWriter(session, firm_id)
        sub_id = await sub.start_trace(
            goal="specialist consult",
            parent_trace_id=parent_id,
            metadata={"specialist": "gst"},
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        sub_row = (
            await session.execute(
                select(AgentTrace).where(AgentTrace.id == sub_id)
            )
        ).scalar_one()
        assert sub_row.parent_trace_id == parent_id
        assert sub_row.metadata_["specialist"] == "gst"
