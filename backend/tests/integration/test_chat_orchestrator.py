"""Orchestrator-level tests for the chat path (post 003d-2).

Exercises ``stream_chat`` against a real test database using a
scripted ``AnthropicClient`` double. Covers the v2 surface:

- A turn with no tool calls (legacy 003d-1 invariant — assistant
  message + trace persisted, no consultation events).
- A turn with one ``consult_specialist`` call (orchestrator + tool
  steps written via ``AgentTraceWriter``, prompt_version_id
  recorded, SSE event ordering).
- A turn with multiple consultations in one orchestrator round.
- A consultation for a specialist that doesn't exist in the firm
  (RLS-driven not-found path — the orchestrator continues with an
  error tool_result instead of crashing).
- Cross-firm isolation: firm B's user cannot trigger consultations
  of firm A's specialists.
- chat_messages.content carries the full displayed text in arrival
  order across all sources.
- agent_trace_steps for consultations record
  ``specialist_prompt_version_id`` in ``content``.
- Threading: prior turns are passed back in conversation history.
- Persistence on streaming error.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.chat.orchestrator import stream_chat
from coworker.connectors.anthropic_client import (
    CompletionMessage,
    StreamCompletion,
    StreamEvent,
    StreamTextDelta,
    StreamToolUseBlock,
)
from coworker.connectors.exceptions import ConnectorTransient
from coworker.db.models import (
    AgentTrace,
    AgentTraceStep,
    ChatConversation,
    ChatMessage,
    Firm,
    Specialist,
    SpecialistPromptVersion,
    User,
)
from coworker.db.session import _attach_pool_listeners, firm_context

_FORCED_RLS_TABLES = (
    "firms",
    "users",
    "audit_log",
    "chat_conversations",
    "chat_messages",
    "specialists",
    "specialist_prompt_versions",
    "agent_traces",
    "agent_trace_steps",
)


# =========================================================================
# Test doubles
# =========================================================================


@dataclass
class OrchRoundScript:
    text_chunks: list[str] = field(default_factory=list)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 100
    output_tokens: int = 30


@dataclass
class SpecResponse:
    text_chunks: list[str]
    input_tokens: int = 200
    output_tokens: int = 80


class _ScriptedAnthropicClient:
    """Fake AnthropicClient: serves canned events from queues.

    Each ``stream_message_with_tools`` call pops one ``OrchRoundScript``;
    each ``stream_message`` call (used by the consultation function)
    pops one ``SpecResponse`` or raises if the entry is an
    ``Exception``.
    """

    def __init__(
        self,
        *,
        orchestrator_rounds: list[OrchRoundScript] | None = None,
        specialist_responses: list[SpecResponse | Exception] | None = None,
    ) -> None:
        self._orch = deque(orchestrator_rounds or [])
        self._spec = deque(specialist_responses or [])
        self.orchestrator_calls: list[dict[str, Any]] = []
        self.specialist_calls: list[dict[str, Any]] = []

    async def stream_message_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None,
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        if not self._orch:
            raise AssertionError(
                "scripted client out of orchestrator rounds; "
                f"got call #{len(self.orchestrator_calls) + 1}"
            )
        script = self._orch.popleft()
        self.orchestrator_calls.append(
            {
                "messages": [dict(m) for m in messages],
                "system": system,
                "tools": tools,
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        for chunk in script.text_chunks:
            yield StreamTextDelta(text=chunk)
        for tu in script.tool_uses:
            yield StreamToolUseBlock(
                id=tu["id"], name=tu["name"], input=tu["input"]
            )
        yield StreamCompletion(
            full_text="".join(script.text_chunks),
            stop_reason=script.stop_reason,
            input_tokens=script.input_tokens,
            output_tokens=script.output_tokens,
            model=model,
        )

    async def stream_message(
        self,
        messages: list[CompletionMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self._spec:
            raise AssertionError(
                "scripted client out of specialist responses; "
                f"got call #{len(self.specialist_calls) + 1}"
            )
        response = self._spec.popleft()
        self.specialist_calls.append(
            {
                "messages": [
                    {"role": m.role, "content": m.content} for m in messages
                ],
                "system": system,
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        if isinstance(response, Exception):
            raise response
        for chunk in response.text_chunks:
            yield StreamTextDelta(text=chunk)
        yield StreamCompletion(
            full_text="".join(response.text_chunks),
            stop_reason="end_turn",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            model=model,
        )


# =========================================================================
# Fixtures
# =========================================================================


@pytest_asyncio.fixture
async def orch_env(test_database_url, monkeypatch) -> AsyncIterator[dict]:
    from coworker.db import session as session_module

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)

    firm_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(
            Firm(
                id=firm_id,
                name="Orch Firm",
                slug=f"orch-{uuid.uuid4().hex[:8]}",
            )
        )
        session.add(
            User(
                id=user_id,
                firm_id=firm_id,
                azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
                upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Orch User",
                role="accountant",
            )
        )
        await session.commit()
    async with sm() as session, firm_context(firm_id):
        conv = ChatConversation(firm_id=firm_id, user_id=user_id)
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
        conv_id = conv.id

    try:
        yield {
            "sm": sm,
            "firm_id": firm_id,
            "user_id": user_id,
            "conv_id": conv_id,
        }
    finally:
        await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id: uuid.UUID) -> None:
    async with sm() as session:
        for t in _FORCED_RLS_TABLES:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            await session.execute(
                text(
                    "UPDATE specialists SET active_version_id = NULL "
                    "WHERE firm_id = :id"
                ),
                {"id": str(firm_id)},
            )
            for t in (
                "agent_trace_steps",
                "agent_traces",
                "chat_messages",
                "chat_conversations",
                "specialist_prompt_versions",
                "specialists",
                "audit_log",
                "users",
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
        for t in _FORCED_RLS_TABLES:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


async def _seed_specialist(
    sm,
    firm_id: uuid.UUID,
    *,
    name: str,
    display_name: str,
    prompt_text: str = "x" * 500,
    model: str = "claude-opus-4-7",
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sm() as session, firm_context(firm_id):
        spec = Specialist(
            firm_id=firm_id,
            name=name,
            display_name=display_name,
            description=f"Description for {name}",
            model=model,
            extended_thinking=True,
        )
        session.add(spec)
        await session.flush()
        version = SpecialistPromptVersion(
            firm_id=firm_id,
            specialist_id=spec.id,
            version_number=1,
            prompt_text=prompt_text,
            status="active",
            change_summary="seed",
        )
        session.add(version)
        await session.flush()
        spec.active_version_id = version.id
        await session.commit()
        return spec.id, version.id


async def _drain(agen):
    out: list[str] = []
    async for chunk in agen:
        out.append(chunk)
    return out


# =========================================================================
# Tests
# =========================================================================


@pytest.mark.asyncio
async def test_orchestrator_no_tool_calls(orch_env):
    """A turn where Sonnet answers directly without consulting any
    specialist. Verifies the legacy 003d-1 invariants survived the
    rewrite: assistant message persisted with restored full text,
    trace + one model_call step recorded, no consultation SSE
    events emitted, token counts on the message row.
    """
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    fake = _ScriptedAnthropicClient(
        orchestrator_rounds=[
            OrchRoundScript(
                text_chunks=["GST ", "stands for ", "Goods and Services Tax."],
                stop_reason="end_turn",
                input_tokens=42,
                output_tokens=17,
            )
        ],
    )

    async with sm() as session, firm_context(firm_id):
        sse_chunks = await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content="What does GST stand for?",
                firm_id=firm_id,
                client=fake,
            )
        )

    assert any("event: token" in c for c in sse_chunks)
    assert any("event: done" in c for c in sse_chunks)
    assert not any("event: error" in c for c in sse_chunks)
    assert not any("specialist_consultation" in c for c in sse_chunks)
    assert all(
        '"source": "orchestrator"' in c or "event: token" not in c
        for c in sse_chunks
    )

    async with sm() as session, firm_context(firm_id):
        msgs = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).scalars().all()
        traces = (
            await session.execute(
                select(AgentTrace).order_by(AgentTrace.started_at.desc())
            )
        ).scalars().all()
        steps = (
            await session.execute(
                select(AgentTraceStep).order_by(AgentTraceStep.step_index.asc())
            )
        ).scalars().all()

    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "GST stands for Goods and Services Tax."
    assert msgs[1].input_tokens == 42
    assert msgs[1].output_tokens == 17
    assert msgs[1].trace_id == traces[0].id
    assert msgs[1].error is None

    assert len(traces) == 1
    assert traces[0].status == "completed"
    assert traces[0].total_input_tokens == 42
    assert traces[0].total_output_tokens == 17
    assert traces[0].num_steps == 1

    assert len(steps) == 1
    assert steps[0].step_type == "model_call"
    assert steps[0].content["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_orchestrator_one_specialist_consultation(orch_env):
    """A turn where Sonnet emits a tool_use → consult_specialist(gst).
    Verifies: SSE event ordering, three trace steps (orch model_call,
    tool_call, tool_result) plus the closing orch model_call, token
    counts aggregated, specialist_prompt_version_id captured.
    """
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    spec_id, version_id = await _seed_specialist(
        sm, firm_id, name="gst", display_name="GST Specialist"
    )

    fake = _ScriptedAnthropicClient(
        orchestrator_rounds=[
            OrchRoundScript(
                text_chunks=["Let me check with the GST Specialist."],
                tool_uses=[
                    {
                        "id": "tool_use_001",
                        "name": "consult_specialist",
                        "input": {
                            "specialist_name": "gst",
                            "question": "GST on going concern sale?",
                        },
                    }
                ],
                stop_reason="tool_use",
                input_tokens=120,
                output_tokens=25,
            ),
            OrchRoundScript(
                text_chunks=[""],
                stop_reason="end_turn",
                input_tokens=900,
                output_tokens=0,
            ),
        ],
        specialist_responses=[
            SpecResponse(
                text_chunks=[
                    "Going concern sale ",
                    "is GST-free under s 38-325.",
                ],
                input_tokens=700,
                output_tokens=40,
            )
        ],
    )

    async with sm() as session, firm_context(firm_id):
        sse_chunks = await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content=(
                    "Selling pharmacy as a going concern — GST?"
                ),
                firm_id=firm_id,
                client=fake,
            )
        )

    joined = "".join(sse_chunks)
    assert "specialist_consultation_started" in joined
    assert "specialist_consultation_complete" in joined
    assert '"source": "orchestrator"' in joined
    assert '"source": "specialist:gst"' in joined
    assert "event: done" in joined
    assert "event: error" not in joined

    # SSE order: orch token(s) → specialist_started → specialist
    # token(s) → specialist_complete → done.
    idx_orch_token = joined.index('"source": "orchestrator"')
    idx_started = joined.index("specialist_consultation_started")
    idx_spec_token = joined.index('"source": "specialist:gst"')
    idx_complete = joined.index("specialist_consultation_complete")
    idx_done = joined.index("event: done")
    assert idx_orch_token < idx_started < idx_spec_token < idx_complete < idx_done

    async with sm() as session, firm_context(firm_id):
        msgs = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).scalars().all()
        traces = (
            await session.execute(
                select(AgentTrace).order_by(AgentTrace.started_at.desc())
            )
        ).scalars().all()
        steps = (
            await session.execute(
                select(AgentTraceStep).order_by(AgentTraceStep.step_index.asc())
            )
        ).scalars().all()

    assert len(traces) == 1
    trace = traces[0]
    assert trace.status == "completed"
    assert trace.num_steps == 4
    assert trace.total_input_tokens == 120 + 700 + 900
    assert trace.total_output_tokens == 25 + 40 + 0

    assert [s.step_type for s in steps] == [
        "model_call",
        "tool_call",
        "tool_result",
        "model_call",
    ]
    assert steps[1].tool_name == "consult_specialist"
    assert steps[1].content["input"]["specialist_name"] == "gst"

    tr = steps[2]
    assert tr.tool_name == "consult_specialist"
    assert tr.model == "claude-opus-4-7"
    assert tr.input_tokens == 700
    assert tr.output_tokens == 40
    assert tr.is_error is False
    assert tr.content["specialist_name"] == "gst"
    assert tr.content["specialist_prompt_version_id"] == str(version_id)

    # Assistant message: displayed text = orch round 1 text + specialist
    # answer + orch round 2 text (empty here).
    assert (
        msgs[1].content
        == "Let me check with the GST Specialist."
        + "Going concern sale is GST-free under s 38-325."
    )
    assert msgs[1].trace_id == trace.id


@pytest.mark.asyncio
async def test_orchestrator_multiple_specialist_consultations(orch_env):
    """One orchestrator round emits two tool_use blocks (gst + smsf);
    both consultations stream; six trace steps total
    (orch, call+result × 2, orch)."""
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    await _seed_specialist(sm, firm_id, name="gst", display_name="GST")
    await _seed_specialist(sm, firm_id, name="smsf", display_name="SMSF")

    fake = _ScriptedAnthropicClient(
        orchestrator_rounds=[
            OrchRoundScript(
                text_chunks=["Two domains here."],
                tool_uses=[
                    {
                        "id": "tu_gst",
                        "name": "consult_specialist",
                        "input": {
                            "specialist_name": "gst",
                            "question": "GST on LRBA?",
                        },
                    },
                    {
                        "id": "tu_smsf",
                        "name": "consult_specialist",
                        "input": {
                            "specialist_name": "smsf",
                            "question": "SMSF compliance for LRBA?",
                        },
                    },
                ],
                stop_reason="tool_use",
            ),
            OrchRoundScript(
                text_chunks=["In short: see both above."],
                stop_reason="end_turn",
            ),
        ],
        specialist_responses=[
            SpecResponse(
                text_chunks=["GST answer."], input_tokens=300, output_tokens=20
            ),
            SpecResponse(
                text_chunks=["SMSF answer."], input_tokens=400, output_tokens=25
            ),
        ],
    )

    async with sm() as session, firm_context(firm_id):
        sse_chunks = await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content="LRBA for related-party commercial property?",
                firm_id=firm_id,
                client=fake,
            )
        )

    joined = "".join(sse_chunks)
    assert joined.count("specialist_consultation_started") == 2
    assert joined.count("specialist_consultation_complete") == 2
    assert '"source": "specialist:gst"' in joined
    assert '"source": "specialist:smsf"' in joined

    async with sm() as session, firm_context(firm_id):
        steps = (
            await session.execute(
                select(AgentTraceStep).order_by(AgentTraceStep.step_index.asc())
            )
        ).scalars().all()
        msgs = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).scalars().all()

    assert [s.step_type for s in steps] == [
        "model_call",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "model_call",
    ]
    # Both consultation results have correct specialist_name
    assert steps[2].content["specialist_name"] == "gst"
    assert steps[4].content["specialist_name"] == "smsf"

    # Displayed text aggregates in order.
    assert msgs[1].content == (
        "Two domains here."
        + "GST answer."
        + "SMSF answer."
        + "In short: see both above."
    )


@pytest.mark.asyncio
async def test_orchestrator_specialist_not_found(orch_env):
    """Sonnet calls consult_specialist with a name not registered for
    the firm. The orchestrator emits a consultation_error SSE event,
    records the tool_result step with is_error=True, and the loop
    continues (Sonnet's next round finishes the turn)."""
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]
    # NO specialists seeded.

    fake = _ScriptedAnthropicClient(
        orchestrator_rounds=[
            OrchRoundScript(
                text_chunks=["Checking..."],
                tool_uses=[
                    {
                        "id": "tu_missing",
                        "name": "consult_specialist",
                        "input": {
                            "specialist_name": "gst",
                            "question": "?",
                        },
                    }
                ],
                stop_reason="tool_use",
            ),
            OrchRoundScript(
                text_chunks=["Sorry, the specialist is unavailable."],
                stop_reason="end_turn",
            ),
        ],
        # No specialist_responses needed — the specialist lookup fails
        # before the streaming call.
    )

    async with sm() as session, firm_context(firm_id):
        sse_chunks = await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content="GST question?",
                firm_id=firm_id,
                client=fake,
            )
        )

    joined = "".join(sse_chunks)
    assert "specialist_consultation_error" in joined
    assert "specialist_consultation_started" not in joined
    assert "event: done" in joined

    async with sm() as session, firm_context(firm_id):
        steps = (
            await session.execute(
                select(AgentTraceStep).order_by(AgentTraceStep.step_index.asc())
            )
        ).scalars().all()

    assert [s.step_type for s in steps] == [
        "model_call",
        "tool_call",
        "tool_result",
        "model_call",
    ]
    tr = steps[2]
    assert tr.is_error is True
    assert "not registered" in tr.content["result"]
    # No prompt_version_id captured since the specialist wasn't found.
    assert "specialist_prompt_version_id" not in tr.content


@pytest.mark.asyncio
async def test_orchestrator_cross_firm_isolation(orch_env):
    """Firm A seeds a 'gst' specialist; firm B's user runs a turn
    that consults 'gst'. RLS hides firm A's row → ConsultationError
    → tool_result with is_error=True. The specialist row is still
    in the DB under firm A."""
    sm = orch_env["sm"]
    firm_a_id = orch_env["firm_id"]
    await _seed_specialist(
        sm, firm_a_id, name="gst", display_name="GST (Firm A)"
    )

    firm_b_id = uuid.uuid4()
    user_b_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_b_id):
        session.add(
            Firm(
                id=firm_b_id,
                name="Firm B",
                slug=f"b-{uuid.uuid4().hex[:8]}",
            )
        )
        session.add(
            User(
                id=user_b_id,
                firm_id=firm_b_id,
                azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
                upn=f"b-{uuid.uuid4().hex[:8]}@example.com",
                display_name="B",
                role="accountant",
            )
        )
        await session.commit()
    async with sm() as session, firm_context(firm_b_id):
        conv_b = ChatConversation(firm_id=firm_b_id, user_id=user_b_id)
        session.add(conv_b)
        await session.commit()
        await session.refresh(conv_b)
        conv_b_id = conv_b.id

    fake = _ScriptedAnthropicClient(
        orchestrator_rounds=[
            OrchRoundScript(
                text_chunks=["Checking..."],
                tool_uses=[
                    {
                        "id": "tu_x",
                        "name": "consult_specialist",
                        "input": {
                            "specialist_name": "gst",
                            "question": "?",
                        },
                    }
                ],
                stop_reason="tool_use",
            ),
            OrchRoundScript(
                text_chunks=["Done."], stop_reason="end_turn"
            ),
        ],
    )

    try:
        async with sm() as session, firm_context(firm_b_id):
            sse_chunks = await _drain(
                stream_chat(
                    session,
                    conversation_id=conv_b_id,
                    user_content="GST?",
                    firm_id=firm_b_id,
                    client=fake,
                )
            )

        joined = "".join(sse_chunks)
        assert "specialist_consultation_error" in joined
        assert "specialist_consultation_started" not in joined

        # Firm A's specialist remains visible under firm A's context.
        async with sm() as session, firm_context(firm_a_id):
            spec_a = (
                await session.execute(
                    select(Specialist).where(Specialist.name == "gst")
                )
            ).scalar_one_or_none()
        assert spec_a is not None
    finally:
        await _cleanup_firm(sm, firm_b_id)


@pytest.mark.asyncio
async def test_orchestrator_threads_prior_messages(orch_env):
    """Two consecutive turns: the second sees the first turn's user
    + assistant messages in api_messages."""
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    first = _ScriptedAnthropicClient(
        orchestrator_rounds=[
            OrchRoundScript(text_chunks=["First answer."])
        ],
    )
    async with sm() as session, firm_context(firm_id):
        await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content="First question",
                firm_id=firm_id,
                client=first,
            )
        )

    second = _ScriptedAnthropicClient(
        orchestrator_rounds=[
            OrchRoundScript(text_chunks=["Second answer."])
        ],
    )
    async with sm() as session, firm_context(firm_id):
        await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content="Second question",
                firm_id=firm_id,
                client=second,
            )
        )

    second_call = second.orchestrator_calls[0]
    # api_messages on the second turn's round 1 = prior history +
    # new user message: user1, assistant1, user2.
    api_msgs = second_call["messages"]
    assert [m["role"] for m in api_msgs] == ["user", "assistant", "user"]
    assert api_msgs[0]["content"] == "First question"
    assert api_msgs[1]["content"] == "First answer."
    assert api_msgs[2]["content"] == "Second question"


@pytest.mark.asyncio
async def test_orchestrator_persists_partial_on_stream_error(orch_env):
    """If the orchestrator's stream raises mid-turn, the assistant
    row is still persisted with whatever was assembled and the
    ``error`` field set. The trace status is ``failed``."""
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    class _ExplodingClient(_ScriptedAnthropicClient):
        async def stream_message_with_tools(self, **kwargs):
            yield StreamTextDelta(text="Partial ")
            yield StreamTextDelta(text="content ")
            raise ConnectorTransient("simulated mid-stream blip")

    fake = _ExplodingClient()

    async with sm() as session, firm_context(firm_id):
        sse_chunks = await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content="Trigger error",
                firm_id=firm_id,
                client=fake,
            )
        )

    joined = "".join(sse_chunks)
    assert "event: error" in joined
    assert "event: done" not in joined

    async with sm() as session, firm_context(firm_id):
        msgs = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).scalars().all()
        traces = (
            await session.execute(select(AgentTrace))
        ).scalars().all()

    assert len(msgs) == 2
    assert msgs[1].error is not None
    assert "ConnectorTransient" in msgs[1].error
    assert traces[0].status == "failed"
    assert traces[0].completion_reason == "connector_error"
