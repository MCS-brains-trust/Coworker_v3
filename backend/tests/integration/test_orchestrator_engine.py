"""Integration tests for ``OrchestratorEngine.run()``.

The model is mocked by ``ScriptedModelCaller`` returning canned
``ModelCallResult`` sequences. The DB layer is real (firm rows,
agent_traces, agent_trace_steps) so RLS + trace persistence are
covered end-to-end. Tools are tiny in-test handlers.
"""
import uuid
from dataclasses import dataclass

import pytest_asyncio
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorTransient,
)
from coworker.db.models import AgentTraceStep, Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.engine import (
    STATUS_BUDGET_EXHAUSTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_MAX_ITERATIONS,
    ModelCallResult,
    OrchestratorEngine,
)
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolError,
    ToolRegistry,
)
from coworker.orchestrator.trace import AgentTraceWriter

# ---------------------------------------------------------------------------
# Scripted model caller
# ---------------------------------------------------------------------------


class ScriptedModelCaller:
    """Returns canned ModelCallResult objects in order."""

    def __init__(self, results: list[ModelCallResult]):
        self._results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, *, messages, system, tools, model, max_tokens, thinking_budget):
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "tools": tools,
                "model": model,
                "thinking_budget": thinking_budget,
            }
        )
        if not self._results:
            raise AssertionError("ScriptedModelCaller exhausted")
        return self._results.pop(0)


def _text_response(text_: str, *, input_tokens=100, output_tokens=20) -> ModelCallResult:
    return ModelCallResult(
        content=[{"type": "text", "text": text_}],
        stop_reason="end_turn",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model="claude-sonnet-4-6",
    )


def _tool_use_response(
    tool_name: str,
    tool_input: dict,
    *,
    tool_use_id: str = "tu_1",
    input_tokens=100,
    output_tokens=30,
) -> ModelCallResult:
    return ModelCallResult(
        content=[
            {"type": "text", "text": "let me check"},
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": tool_input,
            },
        ],
        stop_reason="tool_use",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model="claude-sonnet-4-6",
    )


# ---------------------------------------------------------------------------
# Sample tools
# ---------------------------------------------------------------------------


class _EchoInput(BaseModel):
    text: str = Field(description="Text to echo back.")


async def _echo_handler(inp: _EchoInput, ctx) -> dict:
    return {"echoed": inp.text}


async def _tool_error_handler(inp: _EchoInput, ctx) -> dict:
    raise ToolError("intentional ToolError")


async def _connector_error_handler(inp: _EchoInput, ctx) -> dict:
    raise ConnectorAuthError("simulated 401")


async def _bug_handler(inp: _EchoInput, ctx) -> dict:
    raise TypeError("oops, unexpected")


async def _nonjson_handler(inp: _EchoInput, ctx):
    # Return an object the json encoder can't handle.
    return object()


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine_env(test_database_url):
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


async def _seed_firm(sm) -> tuple[uuid.UUID, Firm]:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id,
            name="Engine Firm",
            slug=f"e-{uuid.uuid4().hex[:8]}",
        )
        session.add(firm)
        await session.commit()
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        session.expunge(firm)
    return firm_id, firm


def _build_registry(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def _echo_tool(name: str = "echo", handler=_echo_handler, side_effect: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} description",
        category="reasoning",
        input_model=_EchoInput,
        handler=handler,
        side_effect=side_effect,
    )


@dataclass
class _RunHarness:
    sm: any
    firm: Firm

    async def run(self, model_caller, *, tools, goal="do the thing", **kwargs):
        async with self.sm() as session, firm_context(self.firm.id):
            attached = await session.merge(self.firm)
            writer = AgentTraceWriter(session, self.firm.id)
            trace_id = await writer.start_trace(goal=goal)

            # Note: an anthropic_client isn't needed for these tests since
            # tools don't use ctx.anthropic; pass a sentinel that satisfies
            # the type at runtime.
            ctx = AgentContext(
                firm=attached,
                session=session,
                anthropic=None,  # type: ignore[arg-type]
                trace_id=trace_id,
                budget_cents=kwargs.pop("budget_cents", None),
            )
            engine = OrchestratorEngine(
                model_caller=model_caller,
                max_iterations=kwargs.pop("max_iterations", 12),
            )
            result = await engine.run(
                ctx,
                goal=goal,
                tools=tools,
                writer=writer,
                **kwargs,
            )
            await session.commit()
            return result


@pytest_asyncio.fixture
async def harness(engine_env):
    firm_id, firm = await _seed_firm(engine_env["sm"])
    engine_env["created"].append(firm_id)
    return _RunHarness(sm=engine_env["sm"], firm=firm)


async def _get_steps(sm, trace_id):
    async with sm() as session:
        await session.execute(
            text(
                "ALTER TABLE agent_trace_steps NO FORCE ROW LEVEL SECURITY"
            )
        )
        await session.commit()
    async with sm() as session:
        result = (
            await session.execute(
                select(AgentTraceStep)
                .where(AgentTraceStep.trace_id == trace_id)
                .order_by(AgentTraceStep.step_index)
            )
        ).scalars().all()
    async with sm() as session:
        await session.execute(
            text("ALTER TABLE agent_trace_steps FORCE ROW LEVEL SECURITY")
        )
        await session.commit()
    return result


# ===========================================================================
# Tests
# ===========================================================================


async def test_single_text_response_completes(harness, engine_env) -> None:
    caller = ScriptedModelCaller([_text_response("hello world")])
    reg = _build_registry(_echo_tool())

    result = await harness.run(caller, tools=reg)

    assert result.status == STATUS_COMPLETED
    assert result.final_text == "hello world"
    assert result.iterations == 1
    assert result.total_input_tokens == 100
    assert result.total_output_tokens == 20

    steps = await _get_steps(engine_env["sm"], result.trace_id)
    assert len(steps) == 1
    assert steps[0].step_type == "model_call"


async def test_single_tool_call_then_final_answer(harness, engine_env) -> None:
    caller = ScriptedModelCaller(
        [
            _tool_use_response("echo", {"text": "ping"}),
            _text_response("done"),
        ]
    )
    reg = _build_registry(_echo_tool())

    result = await harness.run(caller, tools=reg)

    assert result.status == STATUS_COMPLETED
    assert result.final_text == "done"
    assert result.iterations == 2

    steps = await _get_steps(engine_env["sm"], result.trace_id)
    assert [s.step_type for s in steps] == [
        "model_call",
        "tool_call",
        "tool_result",
        "model_call",
    ]
    assert steps[1].tool_name == "echo"
    assert steps[2].content["result"]["echoed"] == "ping"
    assert steps[2].is_error is False


async def test_max_iterations_terminates(harness, engine_env) -> None:
    # Caller always returns a tool_use — loop should hit the cap.
    caller = ScriptedModelCaller(
        [_tool_use_response("echo", {"text": f"x{i}"}, tool_use_id=f"tu{i}")
         for i in range(20)]
    )
    reg = _build_registry(_echo_tool())

    result = await harness.run(caller, tools=reg, max_iterations=3)

    assert result.status == STATUS_MAX_ITERATIONS
    assert result.completion_reason == "max_iterations"
    assert result.iterations == 3


async def test_budget_exhausted_terminates_after_iteration(harness, engine_env) -> None:
    # 100 input + 1000 output @ Sonnet (300/1500 per Mtok) =
    # 300*100/1M + 1500*1000/1M = 30,000 + 1,500,000 -> ceil div by 1M => 2 cents.
    # With budget=1, the FIRST iteration already exceeds it.
    caller = ScriptedModelCaller(
        [
            _tool_use_response(
                "echo", {"text": "x"},
                input_tokens=100, output_tokens=1000,
            ),
            _text_response("never sent"),
        ]
    )
    reg = _build_registry(_echo_tool())

    result = await harness.run(caller, tools=reg, budget_cents=1)

    assert result.status == STATUS_BUDGET_EXHAUSTED
    assert result.completion_reason == "budget_exhausted"
    # One iteration completed (the model_call + the tool dispatch);
    # the budget guard prevented the next iteration.
    assert result.iterations == 1


async def test_unknown_tool_returns_is_error_block(harness, engine_env) -> None:
    caller = ScriptedModelCaller(
        [
            _tool_use_response("missing_tool", {"text": "x"}),
            _text_response("recovered"),
        ]
    )
    reg = _build_registry(_echo_tool())

    result = await harness.run(caller, tools=reg)

    # Loop continued; we ended with the second model_call's text.
    assert result.status == STATUS_COMPLETED
    assert result.final_text == "recovered"

    steps = await _get_steps(engine_env["sm"], result.trace_id)
    # Find the tool_result step — must be is_error with UnknownTool class.
    tool_results = [s for s in steps if s.step_type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert tool_results[0].content["error_class"] == "UnknownTool"


async def test_validation_error_returns_is_error_block(harness, engine_env) -> None:
    caller = ScriptedModelCaller(
        [
            # Missing required 'text' field.
            _tool_use_response("echo", {}),
            _text_response("recovered"),
        ]
    )
    reg = _build_registry(_echo_tool())

    result = await harness.run(caller, tools=reg)

    assert result.status == STATUS_COMPLETED
    steps = await _get_steps(engine_env["sm"], result.trace_id)
    tool_results = [s for s in steps if s.step_type == "tool_result"]
    assert tool_results[0].is_error is True
    assert tool_results[0].content["error_class"] == "ValidationError"


async def test_tool_error_surfaces_as_is_error_and_loop_continues(
    harness, engine_env,
) -> None:
    caller = ScriptedModelCaller(
        [
            _tool_use_response("echo", {"text": "x"}),
            _text_response("ack"),
        ]
    )
    reg = _build_registry(_echo_tool(handler=_tool_error_handler))

    result = await harness.run(caller, tools=reg)

    assert result.status == STATUS_COMPLETED
    steps = await _get_steps(engine_env["sm"], result.trace_id)
    tool_results = [s for s in steps if s.step_type == "tool_result"]
    assert tool_results[0].is_error is True
    assert tool_results[0].content["error_class"] == "ToolError"
    assert "intentional" in tool_results[0].content["result"]["error"]


async def test_connector_error_in_tool_surfaces_with_class(
    harness, engine_env,
) -> None:
    caller = ScriptedModelCaller(
        [
            _tool_use_response("echo", {"text": "x"}),
            _text_response("ack"),
        ]
    )
    reg = _build_registry(_echo_tool(handler=_connector_error_handler))

    result = await harness.run(caller, tools=reg)

    steps = await _get_steps(engine_env["sm"], result.trace_id)
    tool_results = [s for s in steps if s.step_type == "tool_result"]
    assert tool_results[0].is_error is True
    assert tool_results[0].content["error_class"] == "ConnectorAuthError"


async def test_unexpected_exception_in_handler_records_error_step(
    harness, engine_env,
) -> None:
    caller = ScriptedModelCaller(
        [
            _tool_use_response("echo", {"text": "x"}),
            _text_response("ack"),
        ]
    )
    reg = _build_registry(_echo_tool(handler=_bug_handler))

    result = await harness.run(caller, tools=reg)

    steps = await _get_steps(engine_env["sm"], result.trace_id)
    error_steps = [s for s in steps if s.step_type == "error"]
    tool_results = [s for s in steps if s.step_type == "tool_result"]
    # An error step records the bug for ops; the tool_result Claude
    # sees is generic so internals aren't leaked back to the model.
    assert len(error_steps) == 1
    assert error_steps[0].content["error_class"] == "TypeError"
    assert tool_results[0].is_error is True
    assert tool_results[0].content["error_class"] == "InternalError"


async def test_model_call_connector_error_marks_run_failed(
    harness, engine_env,
) -> None:
    class FailingCaller:
        async def __call__(self, **kwargs):
            raise ConnectorTransient("model gateway down")

    reg = _build_registry(_echo_tool())
    result = await harness.run(FailingCaller(), tools=reg)

    assert result.status == STATUS_FAILED
    assert result.completion_reason == "ConnectorTransient"
    steps = await _get_steps(engine_env["sm"], result.trace_id)
    # The model_call never happened; only the error step landed.
    error_steps = [s for s in steps if s.step_type == "error"]
    assert len(error_steps) == 1


async def test_tool_returning_non_json_records_serialisation_error(
    harness, engine_env,
) -> None:
    caller = ScriptedModelCaller(
        [
            _tool_use_response("echo", {"text": "x"}),
            _text_response("ack"),
        ]
    )
    reg = _build_registry(_echo_tool(handler=_nonjson_handler))

    result = await harness.run(caller, tools=reg)

    steps = await _get_steps(engine_env["sm"], result.trace_id)
    tool_results = [s for s in steps if s.step_type == "tool_result"]
    assert tool_results[0].is_error is True
    assert tool_results[0].content["error_class"] == "SerialisationError"


async def test_trace_totals_match_individual_step_costs(
    harness, engine_env,
) -> None:
    caller = ScriptedModelCaller(
        [
            _tool_use_response("echo", {"text": "x"}, input_tokens=200, output_tokens=50),
            _text_response("done", input_tokens=150, output_tokens=40),
        ]
    )
    reg = _build_registry(_echo_tool())

    result = await harness.run(caller, tools=reg)

    assert result.total_input_tokens == 350
    assert result.total_output_tokens == 90
    # Sonnet: 300 cents/Mtok in, 1500 cents/Mtok out
    # Call 1: 200*300/1M + 50*1500/1M = 60_000 + 75_000 = 135_000 -> ceil -> 1 cent
    # Call 2: 150*300/1M + 40*1500/1M = 45_000 + 60_000 = 105_000 -> ceil -> 1 cent
    assert result.total_cost_cents == 2
