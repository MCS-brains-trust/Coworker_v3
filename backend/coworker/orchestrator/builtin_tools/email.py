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

from coworker.approval.items import CreateApprovalInput, create_approval
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
from coworker.security.sanitise import sanitise_and_wrap


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
    """Fetch one message; sanitise + wrap free-text fields.

    Sanitised fields (these come from an attacker-controlled
    inbound email and feed back into Claude via tool_result):
      - subject
      - sender.name + each recipient.name
      - body.content (the full body; max_length 8000 to capture
        a substantive reply without dominating the context)

    Untouched (structured / identifier / boolean):
      - id, conversation_id (Graph-generated UUIDs)
      - sender.email + recipient.email (caller validates)
      - received_at (datetime)
      - is_read, has_attachments (booleans)
      - body.content_type (enum: html / text)
    """
    graph_ctx = _require_graph_ctx(ctx, "email_get_message")
    message = await get_message(graph_ctx, inp.message_id)

    wrapped_subject, _ = sanitise_and_wrap(message.subject, max_length=500)
    wrapped_body, _ = sanitise_and_wrap(
        message.body.content, max_length=8000,
    )

    def _addr(a: Any) -> dict[str, Any]:
        wrapped_name, _ = (
            sanitise_and_wrap(a.name, max_length=200)
            if a.name is not None
            else (None, [])
        )
        return {"email": a.email, "name": wrapped_name}

    return {
        "id": message.id,
        "subject": wrapped_subject,
        "sender": _addr(message.sender) if message.sender else None,
        "to_recipients": [_addr(r) for r in message.to_recipients],
        "cc_recipients": [_addr(r) for r in message.cc_recipients],
        "received_at": message.received_at.isoformat(),
        "body": {
            "content_type": message.body.content_type,
            "content": wrapped_body,
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
# email_propose_draft — write to the Phase 9 approval queue
# ---------------------------------------------------------------------------


class EmailProposeDraftInput(BaseModel):
    """A draft proposal for principal review.

    Same shape as ``email_create_draft`` but lands an approval_item
    row instead of touching Outlook. The Phase 9-4 dispatch sweep
    creates the real Graph draft after the principal approves.
    """

    to: list[str] = Field(
        description="At least one recipient email address.",
        min_length=1,
    )
    subject: str = Field(description="Proposed subject line.")
    body_html: str = Field(
        description=(
            "Proposed HTML body. The principal can edit before "
            "approving via PUT /approval/{id}/payload."
        ),
    )
    summary: str = Field(
        description=(
            "One-line description for the approval inbox. Should "
            "let the principal decide what to do without opening "
            "the item (e.g. 'Reply to Jane Doe — billing question')."
        ),
        max_length=500,
    )
    cc: list[str] | None = Field(default=None)
    bcc: list[str] | None = Field(default=None)
    in_reply_to_message_id: str | None = Field(
        default=None,
        description=(
            "When set, the dispatch sweep uses Graph's createReply + "
            "PATCH path so the eventual draft threads properly."
        ),
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Self-rated confidence in this draft, 0.0 to 1.0. When "
            "above the firm's auto-approve threshold (and the "
            "category isn't two-person), the row is born ``approved`` "
            "and the principal never sees it. Use low values "
            "(0.0-0.6) when uncertain — those always route to human "
            "review."
        ),
    )


async def _email_propose_draft_handler(
    inp: EmailProposeDraftInput, ctx: AgentContext
) -> dict[str, Any]:
    graph_ctx = _require_graph_ctx(ctx, "email_propose_draft")
    payload: dict[str, Any] = {
        "from_user_id": str(graph_ctx.user.id),
        "to": list(inp.to),
        "subject": inp.subject,
        "body_html": inp.body_html,
    }
    if inp.cc:
        payload["cc"] = list(inp.cc)
    if inp.bcc:
        payload["bcc"] = list(inp.bcc)
    if inp.in_reply_to_message_id:
        payload["in_reply_to_message_id"] = inp.in_reply_to_message_id

    row = await create_approval(
        ctx.session,
        ctx.firm.id,
        input=CreateApprovalInput(
            plugin_name=_resolve_plugin_name(ctx),
            category="email_draft",
            summary=inp.summary,
            payload=payload,
            trace_id=ctx.trace_id,
            confidence=inp.confidence,
        ),
    )
    return {
        "approval_item_id": str(row.id),
        "status": row.status,
        "summary": row.summary,
    }


def _resolve_plugin_name(ctx: AgentContext) -> str:
    """Pull the plugin name out of the trace metadata.

    The executor sets ``trace.metadata_['plugin_name']`` when
    starting the trace (see AgentTraceWriter.start_trace).
    Falls back to ``"unknown"`` for traces that didn't go through
    a plugin (e.g. ad-hoc CLI runs) so the approval row is still
    insertable.
    """
    return str(ctx.metadata.get("plugin_name", "unknown"))


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
            name="email_propose_draft",
            description=(
                "Propose a draft reply for principal review. Writes "
                "an approval_item; no Outlook side effect. Once the "
                "principal approves (optionally editing the body "
                "first), the dispatch sweep creates the real draft "
                "in the user's Drafts folder. Prefer this over "
                "email_create_draft for any plugin that wants human "
                "review before sending."
            ),
            category="email",
            input_model=EmailProposeDraftInput,
            handler=_email_propose_draft_handler,
            cost_estimate_cents=0,  # purely DB write
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
