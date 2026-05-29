"""Chat orchestrator (v1: no specialist routing yet).

Streams a Claude response for a conversation given its history.
Writes the user message before the stream; writes the assistant
message after the stream completes (or fails). Yields SSE-formatted
strings for the route handler to forward to the client.

Calls into ``AnthropicClient.stream_message`` so PII scrubbing and
connector taxonomy stay consistent with the rest of the codebase.
Specialist routing (``consult_specialist`` tool) is out of scope
here and lands in Task 003d-2.
"""
import json
import uuid
from collections.abc import AsyncIterator
from typing import Literal, cast

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.config import get_settings
from coworker.connectors.anthropic_client import (
    AnthropicClient,
    CompletionMessage,
    StreamCompletion,
    StreamTextDelta,
)
from coworker.connectors.exceptions import ConnectorError
from coworker.db.models.chat import ChatConversation, ChatMessage

_CompletionRole = Literal["user", "assistant"]

SYSTEM_PROMPT = (
    "You are CoWorker, an AI assistant for an Australian accounting "
    "practice. Be accurate and concise. When tax law or regulatory "
    "questions arise, cite the narrowest useful provision (e.g. "
    "ITAA 1997 s 152-10) and state any assumptions explicitly. If "
    "you are uncertain, say so."
)


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream_chat(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    user_content: str,
    firm_id: uuid.UUID,
    client: AnthropicClient | None = None,
) -> AsyncIterator[str]:
    """Drive one turn of a chat conversation.

    The caller must be inside ``firm_context(firm_id)`` already. Yields
    SSE-formatted strings: ``event: token`` for each text chunk, then
    either ``event: done`` on success or ``event: error`` on failure.
    Both paths persist an assistant message row: on error the row has
    whatever partial text was assembled plus ``error`` populated; on
    success the row has the full restored text and token counts.

    ``client`` is injectable for tests. Production wiring constructs
    a fresh ``AnthropicClient`` scoped to ``firm_id`` per call.
    """
    settings = get_settings()
    if client is None:
        client = AnthropicClient(firm_id=str(firm_id))

    user_msg = ChatMessage(
        conversation_id=conversation_id,
        firm_id=firm_id,
        role="user",
        content=user_content,
    )
    session.add(user_msg)
    await session.flush()

    history_rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.asc())
        )
    ).scalars().all()

    api_messages: list[CompletionMessage] = [
        CompletionMessage(
            role=cast(_CompletionRole, row.role), content=row.content
        )
        for row in history_rows
        if row.role in ("user", "assistant") and row.content
    ]

    assembled: list[str] = []
    full_text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    response_model: str | None = None
    error_text: str | None = None

    try:
        async for event in client.stream_message(
            api_messages,
            model=settings.ANTHROPIC_MODEL_DEFAULT,
            max_tokens=settings.ANTHROPIC_MAX_TOKENS_DEFAULT,
            system=SYSTEM_PROMPT,
        ):
            if isinstance(event, StreamTextDelta):
                assembled.append(event.text)
                yield _sse("token", {"text": event.text})
            elif isinstance(event, StreamCompletion):
                full_text = event.full_text
                input_tokens = event.input_tokens
                output_tokens = event.output_tokens
                response_model = event.model
    except ConnectorError as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "chat.stream_failed: conversation_id={} firm_id={} error={}",
            conversation_id,
            firm_id,
            error_text,
        )
        yield _sse("error", {"error": error_text})
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "chat.stream_unexpected: conversation_id={} firm_id={}",
            conversation_id,
            firm_id,
        )
        yield _sse("error", {"error": error_text})

    assistant_content = (
        full_text if full_text is not None else "".join(assembled)
    )
    assistant_msg = ChatMessage(
        conversation_id=conversation_id,
        firm_id=firm_id,
        role="assistant",
        content=assistant_content,
        model=response_model or settings.ANTHROPIC_MODEL_DEFAULT,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=error_text,
    )
    session.add(assistant_msg)

    conv = (
        await session.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalar_one()
    # SQLAlchemy's ``onupdate=func.now()`` only fires when the row is
    # actually UPDATEd, which it isn't here (we only added child rows).
    # Set it explicitly so list-by-updated_at_desc reflects the turn.
    conv.updated_at = func.now()

    await session.commit()

    if error_text is None:
        yield _sse(
            "done",
            {
                "message_id": str(assistant_msg.id),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
