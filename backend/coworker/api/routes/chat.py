"""HTTP surface for the threaded chat (/api/v1/conversations).

Four endpoints; every handler resolves the authenticated user via
``current_user`` then runs DB work inside ``firm_context(user.firm_id)``
so RLS scopes queries to one firm. Cross-firm conversation ids
return 404 (the RLS predicate hides them; there is no information
leak about "exists but not yours" vs "does not exist").

    POST   /api/v1/conversations                  create a thread
    GET    /api/v1/conversations                  list the user's threads
    GET    /api/v1/conversations/{id}/messages    fetch full history
    POST   /api/v1/conversations/{id}/messages    send a turn (SSE)

The send-message route returns a ``StreamingResponse`` over
``text/event-stream``. The orchestrator persists the user message
before the first SSE frame, streams ``event: token`` frames as
Claude's deltas arrive, and ends with either ``event: done``
(success, with token counts) or ``event: error`` (failure, with the
error string). The assistant message row is written on both paths.
"""
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.api.deps import current_user
from coworker.api.schemas.chat import (
    ChatMessageOut,
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationSummary,
    MessageHistoryResponse,
    MessageSendRequest,
)
from coworker.chat.orchestrator import stream_chat
from coworker.db.models.chat import ChatConversation, ChatMessage
from coworker.db.models.tenancy import User
from coworker.db.session import firm_context, get_session

router = APIRouter(prefix="/api/v1/conversations", tags=["chat"])


@router.post(
    "",
    response_model=ConversationSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: ConversationCreateRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationSummary:
    async with firm_context(user.firm_id):
        conv = ChatConversation(
            firm_id=user.firm_id, user_id=user.id, title=body.title
        )
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
    return ConversationSummary.model_validate(conv)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationListResponse:
    async with firm_context(user.firm_id):
        rows = (
            await session.execute(
                select(ChatConversation).order_by(
                    desc(ChatConversation.updated_at)
                )
            )
        ).scalars().all()
    return ConversationListResponse(
        conversations=[ConversationSummary.model_validate(r) for r in rows]
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=MessageHistoryResponse,
)
async def get_message_history(
    conversation_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageHistoryResponse:
    async with firm_context(user.firm_id):
        conv = (
            await session.execute(
                select(ChatConversation).where(
                    ChatConversation.id == conversation_id
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        rows = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).scalars().all()
    return MessageHistoryResponse(
        messages=[ChatMessageOut.model_validate(r) for r in rows]
    )


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageSendRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    # 404 before opening the stream so the client gets a clean HTTP
    # status rather than a 200 + SSE error frame for an unknown id.
    async with firm_context(user.firm_id):
        conv = (
            await session.execute(
                select(ChatConversation).where(
                    ChatConversation.id == conversation_id
                )
            )
        ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    firm_id = user.firm_id

    async def event_stream() -> AsyncIterator[str]:
        async with firm_context(firm_id):
            async for chunk in stream_chat(
                session,
                conversation_id=conversation_id,
                user_content=body.content,
                firm_id=firm_id,
            ):
                yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Caddy/Nginx-friendly: disable response buffering so the
            # client sees frames as they're emitted, not in a single
            # chunk at end-of-stream.
            "X-Accel-Buffering": "no",
        },
    )
