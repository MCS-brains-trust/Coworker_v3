"""Microsoft Graph mail operations.

Three read entry points so far:

- ``list_inbox`` — page of recent messages with narrow projection.
- ``get_message`` — one full message including body and recipients.
- ``get_attachment`` — one attachment by id, with bytes decoded when
  the type is ``fileAttachment``.

Every call:

- Acquires a slot from the per-process rate limiter (global token
  bucket + per-mailbox semaphore).
- Uses a fixed ``$select`` projection where the endpoint accepts one,
  so we don't ship fields we don't model.
- Maps Graph's response into a frozen Pydantic v2 model. The Graph
  schema is wide and noisy — we expose stable, narrow shapes to
  plugins so they don't grow accidental dependencies on Graph's
  surface.
- Audits ``graph.mail.<action>`` on success with structured payload,
  and ``graph.mail.<action>_failed`` on every non-2xx with a
  ``reason`` field, so the audit chain captures every external-system
  read.
- Normalises HTTP errors into the connector taxonomy
  (``ConnectorAuthError`` / ``ConnectorNotFound`` /
  ``ConnectorRateLimited`` / ``ConnectorTransient``).

Caller invariant: ``ctx.session`` has ``firm_context(ctx.firm.id)``
already entered. ``graph_context`` enters it on the request scope, so
every route consuming these functions is fine.
"""
import base64
import binascii
import datetime as _dt
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from coworker.connectors.exceptions import ConnectorTransient
from coworker.connectors.shadow_mode import guard_writable
from coworker.graph.context import GraphContext
from coworker.graph.errors import (
    audit_failure,
    raise_for_graph_status,
)
from coworker.graph.rate_limit import get_rate_limiter
from coworker.security.audit import append_audit

_MESSAGES_ENDPOINT = "https://graph.microsoft.com/v1.0/me/messages"
_DEFAULT_TOP = 25
_MAX_TOP = 1000  # Microsoft Graph caps $top at 1000 for /me/messages.

# Narrow projection — kept in one place so the InboxMessage schema and
# the wire request stay in sync. If you add a field to InboxMessage,
# add it here.
_SELECT_FIELDS = ",".join([
    "id",
    "subject",
    "from",
    "receivedDateTime",
    "bodyPreview",
    "isRead",
    "hasAttachments",
])

# Fuller projection for `get_message`: full body + all recipient lists
# + conversationId for thread reconstruction in Phase 4 memory.
_FULL_MESSAGE_SELECT_FIELDS = ",".join([
    "id",
    "subject",
    "from",
    "toRecipients",
    "ccRecipients",
    "bccRecipients",
    "receivedDateTime",
    "body",
    "isRead",
    "hasAttachments",
    "conversationId",
])

AttachmentType = Literal["file", "item", "reference", "unknown"]


class InboxAddress(BaseModel):
    """An email participant — either sender or recipient."""

    model_config = ConfigDict(frozen=True)

    email: str
    name: str | None = None


class InboxMessage(BaseModel):
    """A single inbox message, narrowed from Graph's wide schema.

    Stable contract for plugins: the orchestrator's ``email_*`` tools
    will return this shape so plugin code never sees Graph's raw JSON.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    subject: str
    sender: InboxAddress | None = Field(
        default=None,
        description="from sender; None for some calendar/system messages",
    )
    received_at: _dt.datetime
    preview: str
    is_read: bool
    has_attachments: bool


class EmailBody(BaseModel):
    """Message body — content plus its declared type.

    Graph returns ``contentType`` as either ``"html"`` or ``"text"``;
    we preserve that verbatim and leave any sanitisation / conversion
    to the caller (vision, memory, Smart Responder all want different
    things).
    """

    model_config = ConfigDict(frozen=True)

    content_type: Literal["html", "text"]
    content: str


class FullEmailMessage(BaseModel):
    """A single message with full body and all recipients.

    Returned by ``get_message``. Distinct from ``InboxMessage`` so the
    list-view projection (narrow, fast) and the read-one projection
    (wide, complete) can evolve independently.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    subject: str
    sender: InboxAddress | None = None
    to_recipients: list[InboxAddress] = Field(default_factory=list)
    cc_recipients: list[InboxAddress] = Field(default_factory=list)
    bcc_recipients: list[InboxAddress] = Field(default_factory=list)
    received_at: _dt.datetime
    body: EmailBody
    is_read: bool
    has_attachments: bool
    conversation_id: str | None = None


class EmailAttachment(BaseModel):
    """An attachment fetched via ``get_attachment``.

    Graph has three attachment types: ``fileAttachment`` (the common
    case — has ``contentBytes`` base64), ``itemAttachment`` (an
    embedded message / event / contact), and ``referenceAttachment``
    (a link to an external file, e.g. SharePoint). We surface the
    discriminator on ``attachment_type`` and only populate ``content``
    for ``file``. Callers that need item / reference payloads can
    branch on ``attachment_type`` and fetch the specialised endpoint
    in a later phase; for Phase 3 the vision pipeline and the
    correspondence logger only care about file attachments.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    attachment_type: AttachmentType
    name: str
    content_type: str | None
    size: int
    is_inline: bool
    content: bytes | None = Field(
        default=None,
        description=(
            "Decoded bytes for fileAttachment; None for item / "
            "reference / unknown types."
        ),
    )


async def list_inbox(
    ctx: GraphContext, *, top: int = _DEFAULT_TOP
) -> list[InboxMessage]:
    """Return the signed-in user's most recent ``top`` inbox messages.

    Args:
        ctx: per-request Graph bundle. ``ctx.session`` must already
            be inside ``firm_context(ctx.firm.id)``.
        top: page size, 1 ≤ top ≤ 1000. Microsoft caps ``$top`` at
            1000 for /me/messages; pagination beyond that is a
            separate concern landing later in Phase 3 alongside
            ``list_messages`` (delta queries).

    Raises:
        ConnectorAuthError: 401 / 403 from Microsoft.
        ConnectorRateLimited: 429 from Microsoft. Carries
            ``retry_after`` (seconds) when a numeric Retry-After
            header is present.
        ConnectorTransient: 5xx, timeout, or network error.
        ValueError: ``top`` outside [1, 1000].
    """
    if top < 1 or top > _MAX_TOP:
        raise ValueError(f"top must be between 1 and {_MAX_TOP}, got {top}")

    mailbox_id = str(ctx.user.id)
    firm_id_str = str(ctx.firm.id)
    user_id_str = str(ctx.user.id)
    action = "graph.mail.list_inbox"

    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(mailbox_id):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    _MESSAGES_ENDPOINT,
                    params={
                        "$top": top,
                        "$orderby": "receivedDateTime desc",
                        "$select": _SELECT_FIELDS,
                    },
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                )
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=user_id_str,
                action=action,
                reason="network_error",
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    # ``list_inbox`` does not raise ConnectorNotFound — a list endpoint
    # returns 200 with an empty array when nothing matches, not 404.
    await raise_for_graph_status(
        response,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=user_id_str,
        action=action,
        allow_not_found=False,
    )

    body = response.json()
    raw_messages = body.get("value", [])
    messages = [_parse_inbox_message(m) for m in raw_messages]

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action=action,
        payload={
            "user_id": user_id_str,
            "count": len(messages),
            "top": top,
        },
    )
    await ctx.session.commit()

    return messages


async def get_message(ctx: GraphContext, message_id: str) -> FullEmailMessage:
    """Fetch a single message in full, including body and recipients.

    Args:
        ctx: per-request Graph bundle. ``ctx.session`` must already
            be inside ``firm_context(ctx.firm.id)``.
        message_id: Graph message id, as returned by ``list_inbox`` or
            received via webhook. Embedded in the URL path; percent-
            encoded before sending so ids containing ``/`` or ``=``
            are safe.

    Raises:
        ConnectorAuthError: 401 / 403 from Microsoft, or any other
            unhandled 4xx.
        ConnectorNotFound: 404 from Microsoft (message deleted /
            never existed / not visible to this mailbox).
        ConnectorRateLimited: 429 from Microsoft.
        ConnectorTransient: 5xx, timeout, or network error.
        ValueError: ``message_id`` is empty.
    """
    if not message_id:
        raise ValueError("message_id must be non-empty")

    mailbox_id = str(ctx.user.id)
    firm_id_str = str(ctx.firm.id)
    user_id_str = str(ctx.user.id)
    action = "graph.mail.get_message"
    url = f"{_MESSAGES_ENDPOINT}/{quote(message_id, safe='')}"

    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(mailbox_id):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    url,
                    params={"$select": _FULL_MESSAGE_SELECT_FIELDS},
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                )
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=user_id_str,
                action=action,
                reason="network_error",
                extra={"message_id": message_id},
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    await raise_for_graph_status(
        response,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=user_id_str,
        action=action,
        allow_not_found=True,
        extra={"message_id": message_id},
    )

    raw = response.json()
    message = _parse_full_message(raw)

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action=action,
        payload={
            "user_id": user_id_str,
            "message_id": message.id,
            "has_attachments": message.has_attachments,
        },
    )
    await ctx.session.commit()

    return message


async def get_attachment(
    ctx: GraphContext, message_id: str, attachment_id: str
) -> EmailAttachment:
    """Fetch one attachment of a message.

    ``fileAttachment`` content is base64-decoded into ``content``.
    For ``itemAttachment`` and ``referenceAttachment`` the metadata is
    returned with ``content=None``; callers needing the embedded item
    or the reference URL fetch the specialised endpoint themselves
    (out of scope for Phase 3).

    Args:
        ctx: per-request Graph bundle. ``ctx.session`` must already
            be inside ``firm_context(ctx.firm.id)``.
        message_id: parent message id.
        attachment_id: attachment id (as listed in the parent
            message's ``attachments`` collection).

    Raises:
        ConnectorAuthError: 401 / 403 from Microsoft, or any other
            unhandled 4xx.
        ConnectorNotFound: 404 (message or attachment not found).
        ConnectorRateLimited: 429 from Microsoft.
        ConnectorTransient: 5xx, timeout, or network error.
        ValueError: ``message_id`` or ``attachment_id`` is empty, or
            Graph returned a fileAttachment with invalid base64.
    """
    if not message_id:
        raise ValueError("message_id must be non-empty")
    if not attachment_id:
        raise ValueError("attachment_id must be non-empty")

    mailbox_id = str(ctx.user.id)
    firm_id_str = str(ctx.firm.id)
    user_id_str = str(ctx.user.id)
    action = "graph.mail.get_attachment"
    url = (
        f"{_MESSAGES_ENDPOINT}/{quote(message_id, safe='')}"
        f"/attachments/{quote(attachment_id, safe='')}"
    )

    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(mailbox_id):
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                response = await http.get(
                    url,
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                )
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=user_id_str,
                action=action,
                reason="network_error",
                extra={
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                },
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    await raise_for_graph_status(
        response,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=user_id_str,
        action=action,
        allow_not_found=True,
        extra={"message_id": message_id, "attachment_id": attachment_id},
    )

    raw = response.json()
    attachment = _parse_attachment(raw)

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action=action,
        payload={
            "user_id": user_id_str,
            "message_id": message_id,
            "attachment_id": attachment.id,
            "attachment_type": attachment.attachment_type,
            "size": attachment.size,
        },
    )
    await ctx.session.commit()

    return attachment


async def create_draft(
    ctx: GraphContext,
    *,
    to: list[str],
    subject: str,
    body: str,
    body_content_type: Literal["html", "text"] = "html",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
) -> FullEmailMessage:
    """Create a draft email in the user's Drafts folder.

    This is the first write method in the Graph connector and the
    canonical example of the shadow-mode contract: ``guard_writable``
    is called before any HTTP work, so a firm in shadow mode never
    produces an external side effect. If the guard raises, no draft
    is created, no Graph call is made, and a ``shadow_blocked.email.create_draft``
    audit row records the attempt.

    Two shapes:

    - ``in_reply_to is None`` — single POST to ``/me/messages`` with
      the full payload. Graph creates the draft in Drafts and returns
      the resulting Message.
    - ``in_reply_to`` set — two-step path. First POST to
      ``/me/messages/{id}/createReply`` (empty body) which produces a
      threaded draft with the original message quoted; second PATCH
      to ``/me/messages/{draft_id}`` with our payload, replacing
      Graph's "Re: " subject and quoted body. The reply pathway keeps
      proper email threading at the SMTP layer (In-Reply-To /
      References headers); the two-step is unavoidable because the
      single-POST shape doesn't let us set those headers.

    Args:
        ctx: per-request Graph bundle. ``ctx.session`` must already
            be inside ``firm_context(ctx.firm.id)``.
        to: at least one recipient address. Each entry is a bare
            email string (e.g. ``"alice@example.com"``); Graph
            wraps it into the SDK shape on the wire.
        subject: draft subject. Empty allowed.
        body: draft body text. Plaintext for ``body_content_type="text"``,
            HTML otherwise.
        body_content_type: ``"html"`` (default) or ``"text"``.
        cc, bcc: optional recipient lists. ``None`` omits the field
            from the payload entirely; pass ``[]`` to send an
            explicit empty list (rare).
        in_reply_to: message id to reply to. When set, the two-step
            reply path is taken and the resulting draft will thread
            correctly in the recipient's email client.

    Returns:
        ``FullEmailMessage`` parsed from Graph's response — same
        shape as ``get_message`` returns, so callers can pull the
        draft's ``id`` (e.g. to queue for approval), ``web_link``,
        etc. without a second round trip.

    Raises:
        ShadowModeBlocked: ``firm.shadow_mode`` is True. The audit
            row ``shadow_blocked.email.create_draft`` has already been
            committed by ``guard_writable``.
        ConnectorAuthError: 401 / 403 / other unhandled 4xx.
        ConnectorNotFound: 404 from createReply (the ``in_reply_to``
            message doesn't exist or was deleted). For the simple
            path 404 should not occur; if Graph ever returns it,
            it's mapped through the same channel.
        ConnectorRateLimited: 429.
        ConnectorTransient: 5xx, timeout, or network error. If the
            reply path fails at the PATCH step the createReply draft
            remains in Drafts; the caller can delete it manually.
            We do not auto-clean because the audit trail of "what
            we tried" is more valuable than a clean Drafts folder
            during diagnosis.
        ValueError: empty ``to``, any empty address in to/cc/bcc, or
            empty ``in_reply_to`` when provided.
    """
    if not to:
        raise ValueError("to must contain at least one recipient")
    if any(not addr for addr in to):
        raise ValueError("to addresses must be non-empty")
    if cc is not None and any(not addr for addr in cc):
        raise ValueError("cc addresses must be non-empty")
    if bcc is not None and any(not addr for addr in bcc):
        raise ValueError("bcc addresses must be non-empty")
    if in_reply_to is not None and not in_reply_to:
        raise ValueError("in_reply_to must be non-empty when provided")

    mailbox_id = str(ctx.user.id)
    firm_id_str = str(ctx.firm.id)
    user_id_str = str(ctx.user.id)
    action = "graph.mail.create_draft"

    # Shadow guard before any Graph call. guard_writable commits its
    # own audit row inside and raises ShadowModeBlocked if blocked;
    # the rate-limit slot and httpx client below never execute on the
    # blocked path.
    await guard_writable(
        ctx.session,
        ctx.firm,
        action="email.create_draft",
        actor_type="user",
        actor_id=user_id_str,
    )

    draft_payload = _build_draft_payload(
        to=to,
        subject=subject,
        body=body,
        body_content_type=body_content_type,
        cc=cc,
        bcc=bcc,
    )
    audit_extra: dict[str, Any] = {"recipient_count": len(to)}
    if in_reply_to is not None:
        audit_extra["in_reply_to"] = in_reply_to

    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(mailbox_id):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                if in_reply_to is None:
                    response = await http.post(
                        _MESSAGES_ENDPOINT,
                        json=draft_payload,
                        headers={
                            "Authorization": f"Bearer {ctx.access_token}",
                            "Content-Type": "application/json",
                        },
                    )
                    response_for_status = response
                    response_for_status_extra = audit_extra
                else:
                    reply_url = (
                        f"{_MESSAGES_ENDPOINT}/"
                        f"{quote(in_reply_to, safe='')}/createReply"
                    )
                    reply_response = await http.post(
                        reply_url,
                        headers={
                            "Authorization": f"Bearer {ctx.access_token}",
                            "Content-Length": "0",
                        },
                    )
                    # If createReply failed, raise here with step context.
                    await raise_for_graph_status(
                        reply_response,
                        session=ctx.session,
                        firm_id=firm_id_str,
                        user_id=user_id_str,
                        action=action,
                        allow_not_found=True,
                        extra={**audit_extra, "step": "createReply"},
                    )
                    draft_id = reply_response.json()["id"]
                    patch_url = (
                        f"{_MESSAGES_ENDPOINT}/{quote(draft_id, safe='')}"
                    )
                    response = await http.patch(
                        patch_url,
                        json=draft_payload,
                        headers={
                            "Authorization": f"Bearer {ctx.access_token}",
                            "Content-Type": "application/json",
                        },
                    )
                    response_for_status = response
                    response_for_status_extra = {
                        **audit_extra,
                        "draft_id": draft_id,
                        "step": "patch_reply",
                    }
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=user_id_str,
                action=action,
                reason="network_error",
                extra=audit_extra,
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    await raise_for_graph_status(
        response_for_status,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=user_id_str,
        action=action,
        allow_not_found=False,
        extra=response_for_status_extra,
    )

    raw = response.json()
    message = _parse_full_message(raw)

    success_payload: dict[str, Any] = {
        "user_id": user_id_str,
        "draft_id": message.id,
        "recipient_count": len(to),
    }
    if in_reply_to is not None:
        success_payload["in_reply_to"] = in_reply_to

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action=action,
        payload=success_payload,
    )
    await ctx.session.commit()

    return message


def _build_draft_payload(
    *,
    to: list[str],
    subject: str,
    body: str,
    body_content_type: Literal["html", "text"],
    cc: list[str] | None,
    bcc: list[str] | None,
) -> dict[str, Any]:
    """Assemble the Graph draft / patch JSON body.

    Recipients are wrapped into Graph's ``{emailAddress: {address}}``
    shape. ``cc`` and ``bcc`` are only included when explicitly
    provided so callers who don't set them don't accidentally clear
    existing values on a reply PATCH.
    """
    payload: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": body_content_type, "content": body},
        "toRecipients": [_email_address_block(addr) for addr in to],
    }
    if cc is not None:
        payload["ccRecipients"] = [_email_address_block(addr) for addr in cc]
    if bcc is not None:
        payload["bccRecipients"] = [_email_address_block(addr) for addr in bcc]
    return payload


def _email_address_block(address: str) -> dict[str, Any]:
    return {"emailAddress": {"address": address}}


def _parse_inbox_message(raw: dict[str, Any]) -> InboxMessage:
    """Map a single Graph message dict into an ``InboxMessage``.

    Graph's `from` field is sometimes absent (drafts, calendar
    notifications, system mail). The schema permits None there.
    """
    return InboxMessage(
        id=raw["id"],
        subject=raw.get("subject") or "",
        sender=_parse_address(raw.get("from")),
        received_at=_parse_graph_datetime(raw["receivedDateTime"]),
        preview=raw.get("bodyPreview") or "",
        is_read=bool(raw.get("isRead", False)),
        has_attachments=bool(raw.get("hasAttachments", False)),
    )


def _parse_full_message(raw: dict[str, Any]) -> FullEmailMessage:
    """Map a single Graph message dict into a ``FullEmailMessage``."""
    body_block = raw.get("body") or {}
    body_content_type = body_block.get("contentType") or "text"
    if body_content_type not in ("html", "text"):
        # Graph occasionally returns mixed-case ("HTML"); normalise.
        body_content_type = body_content_type.lower()
    if body_content_type not in ("html", "text"):
        # Anything still outside our literal — fall back to text rather
        # than crash on a perfectly readable message.
        body_content_type = "text"
    body = EmailBody(
        content_type=body_content_type,  # type: ignore[arg-type]
        content=body_block.get("content") or "",
    )

    return FullEmailMessage(
        id=raw["id"],
        subject=raw.get("subject") or "",
        sender=_parse_address(raw.get("from")),
        to_recipients=_parse_address_list(raw.get("toRecipients")),
        cc_recipients=_parse_address_list(raw.get("ccRecipients")),
        bcc_recipients=_parse_address_list(raw.get("bccRecipients")),
        received_at=_parse_graph_datetime(raw["receivedDateTime"]),
        body=body,
        is_read=bool(raw.get("isRead", False)),
        has_attachments=bool(raw.get("hasAttachments", False)),
        conversation_id=raw.get("conversationId"),
    )


def _parse_attachment(raw: dict[str, Any]) -> EmailAttachment:
    """Map a single Graph attachment dict into ``EmailAttachment``."""
    odata_type = raw.get("@odata.type", "")
    attachment_type: AttachmentType
    content: bytes | None = None
    if odata_type.endswith("fileAttachment"):
        attachment_type = "file"
        encoded = raw.get("contentBytes")
        if isinstance(encoded, str) and encoded:
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(
                    f"Graph returned invalid base64 for attachment {raw.get('id')!r}"
                ) from exc
    elif odata_type.endswith("itemAttachment"):
        attachment_type = "item"
    elif odata_type.endswith("referenceAttachment"):
        attachment_type = "reference"
    else:
        attachment_type = "unknown"

    return EmailAttachment(
        id=raw["id"],
        attachment_type=attachment_type,
        name=raw.get("name") or "",
        content_type=raw.get("contentType"),
        size=int(raw.get("size") or 0),
        is_inline=bool(raw.get("isInline", False)),
        content=content,
    )


def _parse_address(raw: dict[str, Any] | None) -> InboxAddress | None:
    """Parse a Graph ``{emailAddress: {address, name}}`` block.

    Returns None for missing blocks or blocks without an address.
    """
    if not raw:
        return None
    addr = raw.get("emailAddress") or {}
    email = addr.get("address")
    if not email:
        return None
    return InboxAddress(email=email, name=addr.get("name"))


def _parse_address_list(raw: list[dict[str, Any]] | None) -> list[InboxAddress]:
    """Parse a list of Graph recipient blocks, dropping any without an address."""
    if not raw:
        return []
    parsed = [_parse_address(item) for item in raw]
    return [addr for addr in parsed if addr is not None]


def _parse_graph_datetime(value: str) -> _dt.datetime:
    """Parse a Graph ISO-8601 timestamp into a tz-aware ``datetime``.

    Graph uses trailing ``Z``; ``fromisoformat`` accepts it on Python
    3.12+ but we normalise to ``+00:00`` for clarity and to keep
    parity with Phase 2 helpers.
    """
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


