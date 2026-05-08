"""Microsoft Graph mail operations.

`list_inbox` is the day-one read endpoint: returns the signed-in user's
most recent N messages, ordered by `receivedDateTime` descending.

Every call:

- Acquires a slot from the per-process rate limiter (global token
  bucket + per-mailbox semaphore).
- Hits ``GET /me/messages`` with a fixed ``$select`` projection so we
  don't ship megabytes of HTML body across the wire just to render an
  inbox list. Phase 4 will introduce a separate ``get_message`` for
  the full body when the orchestrator needs it.
- Maps Graph's response into ``InboxMessage`` (Pydantic v2). The
  Graph schema is wide and noisy — we expose a stable, narrow shape
  to plugins so they don't grow accidental dependencies on Graph's
  surface.
- Audits ``graph.mail.list_inbox`` with the count and ``$top``
  parameter so the audit chain captures every external-system read.
- Normalises HTTP errors into the connector taxonomy
  (``ConnectorAuthError`` / ``ConnectorRateLimited`` /
  ``ConnectorTransient``).

Caller invariant: ``ctx.session`` has ``firm_context(ctx.firm.id)``
already entered. ``graph_context`` enters it on the request scope, so
every route consuming this function is fine.
"""
import datetime as _dt
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.graph.context import GraphContext
from coworker.graph.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
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
            await _audit_failure(
                ctx.session, firm_id_str, user_id_str, "network_error"
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    status = response.status_code

    if status == 401 or status == 403:
        await _audit_failure(
            ctx.session, firm_id_str, user_id_str, f"microsoft_{status}"
        )
        raise ConnectorAuthError(
            f"Microsoft Graph rejected request: HTTP {status}"
        )
    if status == 429:
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        await _audit_failure(
            ctx.session, firm_id_str, user_id_str, "microsoft_429"
        )
        raise ConnectorRateLimited(retry_after=retry_after)
    if 500 <= status < 600:
        await _audit_failure(
            ctx.session, firm_id_str, user_id_str, "microsoft_5xx"
        )
        raise ConnectorTransient(
            f"Microsoft Graph returned {status}"
        )
    if status >= 400:
        # Anything else 4xx (e.g. 400 bad query) — treat as auth-class
        # for now (caller can't recover automatically). XPM/FuseSign
        # connectors may grow a finer-grained ConnectorPermanent later.
        await _audit_failure(
            ctx.session, firm_id_str, user_id_str, f"microsoft_{status}"
        )
        raise ConnectorAuthError(
            f"Microsoft Graph returned {status}"
        )

    body = response.json()
    raw_messages = body.get("value", [])
    messages = [_parse_message(m) for m in raw_messages]

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action="graph.mail.list_inbox",
        payload={
            "user_id": user_id_str,
            "count": len(messages),
            "top": top,
        },
    )
    await ctx.session.commit()

    return messages


def _parse_message(raw: dict[str, Any]) -> InboxMessage:
    """Map a single Graph message dict into an ``InboxMessage``.

    Graph's `from` field is sometimes absent (drafts, calendar
    notifications, system mail). The schema permits None there.
    """
    sender_block = raw.get("from")
    sender: InboxAddress | None = None
    if sender_block:
        addr = sender_block.get("emailAddress", {})
        email = addr.get("address")
        if email:
            sender = InboxAddress(email=email, name=addr.get("name"))

    return InboxMessage(
        id=raw["id"],
        subject=raw.get("subject") or "",
        sender=sender,
        received_at=_dt.datetime.fromisoformat(
            raw["receivedDateTime"].replace("Z", "+00:00")
        ),
        preview=raw.get("bodyPreview") or "",
        is_read=bool(raw.get("isRead", False)),
        has_attachments=bool(raw.get("hasAttachments", False)),
    )


def _parse_retry_after(header: str | None) -> float | None:
    """Parse Retry-After. Microsoft Graph returns integer seconds.

    Returns None for missing or non-numeric headers (HTTP-date form
    is rare from Graph and we don't bother parsing it; the caller
    sees ``retry_after=None`` and applies its own default backoff).
    """
    if header is None:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        return None


async def _audit_failure(
    session: AsyncSession, firm_id: str, user_id: str, reason: str
) -> None:
    """Append a ``graph.mail.list_inbox_failed`` entry and commit.

    Same pattern as `refresh_access_token`: failure audits commit
    inside the helper so they survive any subsequent rollback in the
    request scope (FastAPI exception propagation may discard the
    session before it commits).
    """
    await append_audit(
        session,
        firm_id=firm_id,
        actor_type="user",
        actor_id=user_id,
        action="graph.mail.list_inbox_failed",
        payload={"user_id": user_id, "reason": reason},
    )
    await session.commit()
