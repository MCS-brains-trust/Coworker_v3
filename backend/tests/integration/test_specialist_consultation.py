"""Unit-ish tests for ``coworker.chat.specialist_consultation``.

These run against the real test database (so RLS-scoped specialist
lookup is exercised) but stub out the Anthropic client. Two
invariants:

1. The consultation loads the *currently active* prompt version
   (not a retired one), and records its id on the Complete event.
2. A streaming error mid-consultation produces a
   ``ConsultationError`` carrying the partial assembled text and
   the prompt_version_id (since the version *was* loaded before
   the error).
"""
from __future__ import annotations

import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.chat.specialist_consultation import (
    ConsultationComplete,
    ConsultationError,
    ConsultationStarted,
    ConsultationTextDelta,
    consult_specialist,
)
from coworker.connectors.anthropic_client import (
    CompletionMessage,
    StreamCompletion,
    StreamEvent,
    StreamTextDelta,
)
from coworker.connectors.exceptions import ConnectorTransient
from coworker.db.models import Firm, Specialist, SpecialistPromptVersion
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


@dataclass
class _Resp:
    text_chunks: list[str]
    input_tokens: int = 100
    output_tokens: int = 30


class _StubClient:
    """Minimal AnthropicClient surface used by consult_specialist:
    only ``stream_message`` is touched (specialists do not use tools).
    """

    def __init__(self, *, responses: list[_Resp | Exception]) -> None:
        self._q = deque(responses)
        self.calls: list[dict[str, Any]] = []

    async def stream_message(
        self,
        messages: list[CompletionMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self._q:
            raise AssertionError("stub out of scripted responses")
        item = self._q.popleft()
        self.calls.append(
            {
                "messages": [
                    {"role": m.role, "content": m.content} for m in messages
                ],
                "system": system,
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        if isinstance(item, Exception):
            raise item
        for chunk in item.text_chunks:
            yield StreamTextDelta(text=chunk)
        yield StreamCompletion(
            full_text="".join(item.text_chunks),
            stop_reason="end_turn",
            input_tokens=item.input_tokens,
            output_tokens=item.output_tokens,
            model=model,
        )


@pytest_asyncio.fixture
async def consult_env(test_database_url, monkeypatch) -> AsyncIterator[dict]:
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
    async with sm() as session, firm_context(firm_id):
        session.add(
            Firm(
                id=firm_id,
                name="Consult Firm",
                slug=f"con-{uuid.uuid4().hex[:8]}",
            )
        )
        await session.commit()

    try:
        yield {"sm": sm, "firm_id": firm_id}
    finally:
        await _cleanup(sm, firm_id)
        await engine.dispose()


async def _cleanup(sm, firm_id: uuid.UUID) -> None:
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


async def _seed_specialist_with_versions(
    sm,
    firm_id: uuid.UUID,
    *,
    name: str,
    display_name: str,
    versions: list[tuple[str, str]],  # list of (prompt_text, status)
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    version_ids: list[uuid.UUID] = []
    async with sm() as session, firm_context(firm_id):
        spec = Specialist(
            firm_id=firm_id,
            name=name,
            display_name=display_name,
            description=f"Description for {name}",
            model="claude-opus-4-7",
            extended_thinking=True,
        )
        session.add(spec)
        await session.flush()
        active_id: uuid.UUID | None = None
        for i, (text_body, status) in enumerate(versions, start=1):
            v = SpecialistPromptVersion(
                firm_id=firm_id,
                specialist_id=spec.id,
                version_number=i,
                prompt_text=text_body,
                status=status,
                change_summary=f"version {i}",
            )
            session.add(v)
            await session.flush()
            version_ids.append(v.id)
            if status == "active":
                active_id = v.id
        spec.active_version_id = active_id
        await session.commit()
        return spec.id, version_ids


async def _drain(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_consultation_uses_active_prompt_version(consult_env):
    """Seed v1 retired + v2 active. The consultation loads v2's
    prompt text into the system field and records v2's id on the
    Started + Complete events."""
    sm = consult_env["sm"]
    firm_id = consult_env["firm_id"]

    spec_id, version_ids = await _seed_specialist_with_versions(
        sm,
        firm_id,
        name="gst",
        display_name="GST Specialist",
        versions=[
            ("Retired v1 prompt text " * 10, "retired"),
            ("Active v2 prompt text " * 10, "active"),
        ],
    )
    v1_id, v2_id = version_ids

    client = _StubClient(responses=[_Resp(text_chunks=["GST answer."])])

    async with sm() as session, firm_context(firm_id):
        events = await _drain(
            consult_specialist(
                session,
                client,
                specialist_name="gst",
                question="GST on going concern?",
            )
        )

    types = [type(e).__name__ for e in events]
    assert types == [
        "ConsultationStarted",
        "ConsultationTextDelta",
        "ConsultationComplete",
    ]

    started = events[0]
    assert isinstance(started, ConsultationStarted)
    assert started.prompt_version_id == v2_id
    assert started.model == "claude-opus-4-7"

    complete = events[-1]
    assert isinstance(complete, ConsultationComplete)
    assert complete.prompt_version_id == v2_id
    assert complete.full_text == "GST answer."

    # The system prompt sent to the SDK = v2's prompt_text.
    assert client.calls[0]["system"].startswith("Active v2 prompt text")


@pytest.mark.asyncio
async def test_consultation_handles_anthropic_error(consult_env):
    """A streaming error after the consultation has begun. The
    ConsultationError event carries the partial assembled text
    and the prompt_version_id (the version *was* loaded)."""
    sm = consult_env["sm"]
    firm_id = consult_env["firm_id"]

    _, [active_id] = await _seed_specialist_with_versions(
        sm,
        firm_id,
        name="smsf",
        display_name="SMSF Specialist",
        versions=[("Active SMSF prompt " * 10, "active")],
    )

    class _PartialThenRaise:
        async def stream_message(
            self, messages, *, model, max_tokens, system=None
        ):
            yield StreamTextDelta(text="Starting analysis ")
            yield StreamTextDelta(text="of LRBA structure ")
            raise ConnectorTransient("upstream timeout")

    async with sm() as session, firm_context(firm_id):
        events = await _drain(
            consult_specialist(
                session,
                _PartialThenRaise(),
                specialist_name="smsf",
                question="LRBA?",
            )
        )

    # Started, two deltas, then Error (no Complete).
    types = [type(e).__name__ for e in events]
    assert types == [
        "ConsultationStarted",
        "ConsultationTextDelta",
        "ConsultationTextDelta",
        "ConsultationError",
    ]

    err = events[-1]
    assert isinstance(err, ConsultationError)
    assert err.specialist_name == "smsf"
    assert err.prompt_version_id == active_id
    assert err.model == "claude-opus-4-7"
    assert err.partial_text == "Starting analysis of LRBA structure "
    assert "ConnectorTransient" in err.error


@pytest.mark.asyncio
async def test_consultation_missing_specialist_errors_cleanly(consult_env):
    """No specialist row in the firm: a single ConsultationError event,
    no Started or deltas."""
    sm = consult_env["sm"]
    firm_id = consult_env["firm_id"]

    client = _StubClient(responses=[])  # never called

    async with sm() as session, firm_context(firm_id):
        events = await _drain(
            consult_specialist(
                session,
                client,
                specialist_name="cgt_concessions_rollovers",
                question="anything",
            )
        )

    assert len(events) == 1
    err = events[0]
    assert isinstance(err, ConsultationError)
    assert "not registered" in err.error
    assert err.prompt_version_id is None
    assert err.model is None
    assert client.calls == []
