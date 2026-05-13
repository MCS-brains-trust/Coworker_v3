"""Email-category builtin tools.

Three tools wrap the Phase 3C-1 / 3C-4 / 3C-5 Graph mail
functions for use from inside an agent loop:

- ``email_get_message`` (read) — fetch a full message including
  body and recipients.
- ``email_create_draft`` (write, shadow-guarded at the connector
  layer) — produce a draft reply with optional ``in_reply_to``
  threading.
- ``email_mark_as_read`` (write, shadow-guarded) — idempotently
  flip a message's read state.

All three require ``ctx.graph_ctx`` (a per-user GraphContext from
Phase 3C-4). When absent, the handler raises ToolError so the
agent loop continues with a Claude-visible error rather than
crashing. Plugins that need these tools must run inside a context
the webhook receiver / worker pool has hydrated with the right
mailbox owner's Graph credentials.
"""
from typing import Any, Literal

from pydantic import BaseModel, Field

from coworker.graph.context import GraphContext
from coworker.graph.mail import (
    create_draft,
    get_message,
    mark_as_read,
)
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolError,
    ToolRegistry,
)


def _require_graph_ctx(ctx: AgentContext, tool_name: str) -> GraphContext:
    """Return ctx.graph_ctx or raise ToolError with a clear message."""
    if ctx.graph_ctx is None:
        raise ToolError(
            f"{tool_name} requires a Microsoft Graph context, which "
            "isn't available in this run (the orchestrator wasn't "
            "given a mailbox owner). Continue without it or escalate "
            "to a human."
        )
    return ctx.graph_ctx


# ---------------------------------------------------------------------------
# email_get_message
# ---------------------------------------------------------------------------


class EmailGetMessageInput(BaseModel):
    message_id: str = Field(
        description=(
            "Microsoft Graph message id (as returned by list_inbox or "
            "received via webhook). URL-encoded by the underlying "
            "client so ids containing / or = are safe."
        )
    )


async def _email_get_message_handler(
    inp: EmailGetMessageInput, ctx: AgentContext
) -> dict[str, Any]:
    graph_ctx = _require_graph_ctx(ctx, "email_get_message")
    message = await get_message(graph_ctx, inp.message_id)
    return {
        "id": message.id,
        "subject": message.subject,
        "sender": (
            {"email": message.sender.email, "name": message.sender.name}
            if message.sender
            else None
        ),
        "to_recipients": [
            {"email": r.email, "name": r.name}
            for r in message.to_recipients
        ],
        "cc_recipients": [
            {"email": r.email, "name": r.name}
            for r in message.cc_recipients
        ],
        "received_at": message.received_at.isoformat(),
        "body": {
            "content_type": message.body.content_type,
            "content": message.body.content,
        },
        "is_read": message.is_read,
        "has_attachments": message.has_attachments,
        "conversation_id": message.conversation_id,
    }


# ---------------------------------------------------------------------------
# email_create_draft
# ---------------------------------------------------------------------------


class EmailCreateDraftInput(BaseModel):
    to: list[str] = Field(
        description=(
            "At least one recipient email address. The connector "
            "wraps each into Graph's emailAddress block."
        ),
        min_length=1,
    )
    subject: str = Field(description="Draft subject line.")
    body: str = Field(
        description=(
            "Draft body. HTML by default; pass body_content_type='text' "
            "for plaintext."
        )
    )
    body_content_type: Literal["html", "text"] = Field(
        default="html",
        description="Whether body is HTML or plaintext.",
    )
    cc: list[str] | None = Field(
        default=None,
        description="Optional CC recipients.",
    )
    bcc: list[str] | None = Field(
        default=None,
        description="Optional BCC recipients.",
    )
    in_reply_to: str | None = Field(
        default=None,
        description=(
            "When set, the connector uses Graph's createReply + "
            "PATCH path so the draft threads properly. Pass the id "
            "of the message you're replying to."
        ),
    )


async def _email_create_draft_handler(
    inp: EmailCreateDraftInput, ctx: AgentContext
) -> dict[str, Any]:
    graph_ctx = _require_graph_ctx(ctx, "email_create_draft")
    message = await create_draft(
        graph_ctx,
        to=inp.to,
        subject=inp.subject,
        body=inp.body,
        body_content_type=inp.body_content_type,
        cc=inp.cc,
        bcc=inp.bcc,
        in_reply_to=inp.in_reply_to,
    )
    return {
        "draft_id": message.id,
        "subject": message.subject,
        "to_recipients": [
            {"email": r.email, "name": r.name}
            for r in message.to_recipients
        ],
        "conversation_id": message.conversation_id,
    }


# ---------------------------------------------------------------------------
# email_mark_as_read
# ---------------------------------------------------------------------------


class EmailMarkAsReadInput(BaseModel):
    message_id: str = Field(
        description="Message to mark as read."
    )


async def _email_mark_as_read_handler(
    inp: EmailMarkAsReadInput, ctx: AgentContext
) -> dict[str, Any]:
    graph_ctx = _require_graph_ctx(ctx, "email_mark_as_read")
    await mark_as_read(graph_ctx, inp.message_id)
    return {"message_id": inp.message_id, "is_read": True}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="email_get_message",
            description=(
                "Fetch a single email by id, including body and "
                "recipients. Use when the body preview wasn't enough "
                "to understand the request."
            ),
            category="email",
            input_model=EmailGetMessageInput,
            handler=_email_get_message_handler,
            cost_estimate_cents=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="email_create_draft",
            description=(
                "Create a draft reply in the user's Drafts folder. "
                "Set in_reply_to to thread properly. Shadow-guarded "
                "at the connector layer: in a firm running in shadow "
                "mode, no draft is created and the would-be content "
                "lands in the audit log instead."
            ),
            category="email",
            input_model=EmailCreateDraftInput,
            handler=_email_create_draft_handler,
            cost_estimate_cents=2,  # connector + small write
            side_effect=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="email_mark_as_read",
            description=(
                "Mark an email as read. Idempotent; safe to call "
                "even if the message is already read. Shadow-guarded."
            ),
            category="email",
            input_model=EmailMarkAsReadInput,
            handler=_email_mark_as_read_handler,
            cost_estimate_cents=0,
            side_effect=True,
        )
    )
