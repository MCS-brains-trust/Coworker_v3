"""Orchestrator-level tests for the chat path.

These tests exercise ``stream_chat`` directly against the real test
database, using a stubbed ``AnthropicClient`` so no API calls leave
the process. They cover the two persistence invariants that matter
to v1: a successful stream writes both the user and assistant rows
with token counts and the restored full text; a streaming error
still writes an assistant row with the assembled partial text and
the ``error`` field populated.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

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
)
from coworker.connectors.exceptions import ConnectorTransient
from coworker.db.models import ChatConversation, ChatMessage, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context

_FORCED_RLS_TABLES = (
    "firms",
    "users",
    "audit_log",
    "chat_conversations",
    "chat_messages",
)


class _FakeAnthropicClient:
    """Test double matching the subset of ``AnthropicClient`` that
    ``stream_chat`` actually calls.
    """

    def __init__(
        self,
        *,
        chunks: list[str] | None = None,
        input_tokens: int = 11,
        output_tokens: int = 22,
        model: str = "claude-sonnet-4-6",
        raise_after: int | None = None,
    ) -> None:
        self._chunks = chunks or ["Hello", ", ", "world."]
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._model = model
        self._raise_after = raise_after
        self.received_messages: list[CompletionMessage] = []
        self.received_system: str | None = None

    async def stream_message(
        self,
        messages: list[CompletionMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.received_messages = list(messages)
        self.received_system = system
        for i, chunk in enumerate(self._chunks):
            if self._raise_after is not None and i >= self._raise_after:
                raise ConnectorTransient("simulated network blip")
            yield StreamTextDelta(text=chunk)
        yield StreamCompletion(
            full_text="".join(self._chunks),
            stop_reason="end_turn",
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            model=self._model,
        )


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
                id=firm_id, name="Orch Firm",
                slug=f"orch-{uuid.uuid4().hex[:8]}",
            )
        )
        session.add(
            User(
                id=user_id, firm_id=firm_id,
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
            for t in (
                "chat_messages",
                "chat_conversations",
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


async def _drain(agen):
    out: list[str] = []
    async for chunk in agen:
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_stream_chat_persists_user_and_assistant_messages(orch_env):
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    fake = _FakeAnthropicClient(
        chunks=["Division ", "7A handles ", "shareholder loans."],
        input_tokens=42,
        output_tokens=17,
    )

    async with sm() as session, firm_context(firm_id):
        sse_chunks = await _drain(
            stream_chat(
                session,
                conversation_id=conv_id,
                user_content="What is Division 7A?",
                firm_id=firm_id,
                client=fake,
            )
        )

    # SSE: three token events + one done event
    assert any("event: token" in chunk for chunk in sse_chunks)
    assert any("event: done" in chunk for chunk in sse_chunks)
    assert not any("event: error" in chunk for chunk in sse_chunks)

    # The fake was called with one user message; system prompt is set.
    assert len(fake.received_messages) == 1
    assert fake.received_messages[0].role == "user"
    assert fake.received_messages[0].content == "What is Division 7A?"
    assert fake.received_system is not None

    # Two rows persisted: the user message and the assistant message.
    async with sm() as session, firm_context(firm_id):
        rows = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).scalars().all()

    assert len(rows) == 2
    user_row, assistant_row = rows
    assert user_row.role == "user"
    assert user_row.content == "What is Division 7A?"
    assert user_row.input_tokens is None
    assert user_row.output_tokens is None
    assert user_row.model is None
    assert user_row.error is None

    assert assistant_row.role == "assistant"
    assert assistant_row.content == "Division 7A handles shareholder loans."
    assert assistant_row.input_tokens == 42
    assert assistant_row.output_tokens == 17
    assert assistant_row.model == "claude-sonnet-4-6"
    assert assistant_row.error is None


@pytest.mark.asyncio
async def test_stream_chat_on_anthropic_error_persists_partial_with_error_field(
    orch_env,
):
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    # Two chunks arrive, then the client raises on the third.
    fake = _FakeAnthropicClient(
        chunks=["Partial ", "answer ", "before error"],
        raise_after=2,
    )

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

    # An error frame was emitted; no done frame was emitted.
    assert any("event: error" in chunk for chunk in sse_chunks)
    assert not any("event: done" in chunk for chunk in sse_chunks)

    async with sm() as session, firm_context(firm_id):
        rows = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).scalars().all()

    assert len(rows) == 2
    user_row, assistant_row = rows
    assert user_row.role == "user"
    assert user_row.content == "Trigger error"

    assert assistant_row.role == "assistant"
    assert assistant_row.content == "Partial answer "
    assert assistant_row.input_tokens is None
    assert assistant_row.output_tokens is None
    assert assistant_row.error is not None
    assert "ConnectorTransient" in assistant_row.error


@pytest.mark.asyncio
async def test_stream_chat_threads_prior_messages_in_history(orch_env):
    """A second turn passes BOTH the prior user/assistant messages and
    the new user message to the client."""
    sm = orch_env["sm"]
    firm_id = orch_env["firm_id"]
    conv_id = orch_env["conv_id"]

    first = _FakeAnthropicClient(chunks=["First answer."])
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

    second = _FakeAnthropicClient(chunks=["Second answer."])
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

    # Second call saw three messages: user1, assistant1, user2.
    assert [m.role for m in second.received_messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert [m.content for m in second.received_messages] == [
        "First question",
        "First answer.",
        "Second question",
    ]
