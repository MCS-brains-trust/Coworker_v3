"""Dispatch approved email-draft approval items to Outlook.

When the principal approves an ``email_draft`` item, the approval
state machine moves to ``approved`` but nothing has yet landed in
Outlook. ``dispatch_email_draft`` (per-item) and
``sweep_dispatch`` (platform-wide) close that loop: each
``approved`` item produces a real Microsoft Graph draft in the
sender's Drafts folder, and the row transitions to ``sent``.

We never auto-Send — the architecture rule "Drafts only" stands.
``sent`` here means "we've done our part; the draft is in the
user's Outlook ready for them to click Send." If the principal
opens Outlook and Sends, the recipient receives the email; if
they delete the draft instead, that's the principal's choice.

Per-item failure handling:
- Shadow mode blocks ``create_draft`` (the architecture's
  enforcement boundary). We record ``dispatch_failed`` so the
  trace is visible; once the firm graduates shadow mode the next
  sweep retries.
- ConnectorAuthError / ConnectorRateLimited / ConnectorTransient
  -> ``dispatch_failed``. The dispatch sweep walks these on the
  next tick.
- Any other unexpected exception -> ``dispatch_failed`` + logged.

Payload contract for ``email_draft`` category::

    {
        "from_user_id": "<uuid>",      # sender mailbox owner
        "to": ["..."],                  # recipients
        "cc": ["..."],                  # optional
        "bcc": ["..."],                 # optional
        "subject": "...",
        "body_html": "...",
        "in_reply_to_message_id": "..." # optional; threads reply
    }

Missing/invalid ``from_user_id`` -> ``dispatch_failed`` with a
clear reason — the producing plugin should be fixed.
"""
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.connectors.exceptions import ConnectorError
from coworker.connectors.shadow_mode import ShadowModeBlocked
from coworker.db.firms import list_active_firm_ids
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import firm_context
from coworker.graph.context import GraphContext
from coworker.graph.mail import FullEmailMessage, create_draft
from coworker.graph.user_context import resolve_user_graph_context


@dataclass
class DispatchResult:
    """Per-sweep dispatch summary.

    Counts let dashboards alert on regressions without parsing
    per-item logs.
    """

    firms_seen: int = 0
    items_seen: int = 0
    dispatched: int = 0
    failed: int = 0
    actions: dict[str, int] = field(default_factory=dict)

    def record(self, action: str) -> None:
        self.actions[action] = self.actions.get(action, 0) + 1


async def dispatch_email_draft(
    *,
    session: AsyncSession,
    item: ApprovalItem,
    now: _dt.datetime | None = None,
) -> str:
    """Create the Outlook draft for one approved email_draft item.

    Returns the action label written into the result counters:
    ``dispatched`` on success, ``shadow_blocked`` / ``bad_payload``
    / ``user_missing`` / ``user_no_token`` / ``connector_error`` /
    ``crashed`` on each failure mode. The row is transitioned to
    ``sent`` on success or ``dispatch_failed`` on any failure;
    caller commits.
    """
    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    payload = item.payload

    from_user_id_raw = payload.get("from_user_id")
    if not isinstance(from_user_id_raw, str):
        return await _mark_failed(item, "bad_payload_no_sender", now)
    try:
        from_user_id = uuid.UUID(from_user_id_raw)
    except ValueError:
        return await _mark_failed(item, "bad_payload_no_sender", now)

    to = payload.get("to")
    subject = payload.get("subject")
    body_html = payload.get("body_html")
    if (
        not isinstance(to, list) or not to
        or not isinstance(subject, str)
        or not isinstance(body_html, str)
    ):
        return await _mark_failed(item, "bad_payload_shape", now)

    user = (
        await session.execute(
            select(User).where(User.id == from_user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        return await _mark_failed(item, "user_missing", now)

    firm = (
        await session.execute(
            select(Firm).where(Firm.id == item.firm_id)
        )
    ).scalar_one()

    ctx = await resolve_user_graph_context(session, firm=firm, user=user)
    if ctx is None:
        return await _mark_failed(item, "user_no_token", now)

    try:
        draft = await _create_outlook_draft(
            ctx, payload=payload, body_html=body_html,
        )
    except ShadowModeBlocked:
        return await _mark_failed(item, "shadow_blocked", now)
    except ConnectorError as exc:
        logger.warning(
            "dispatch connector error item_id={} err={}",
            item.id, exc,
        )
        return await _mark_failed(item, "connector_error", now)
    except Exception:
        logger.exception("dispatch crashed item_id={}", item.id)
        return await _mark_failed(item, "crashed", now)

    item.status = "sent"
    item.updated_at = now
    # Task 3: capture the draft's proposed RFC 5322 Message-ID so an
    # incoming NDR can be correlated back to this row, and flip
    # delivery_status to 'sent' (the 4h confirmation sweep watches
    # delivery_status_updated_at for the flip to 'delivered').
    item.executed_internet_message_id = draft.internet_message_id
    item.delivery_status = "sent"
    item.delivery_status_updated_at = now
    await session.flush()
    logger.info(
        "dispatch sent item_id={} internet_message_id={!r}",
        item.id, draft.internet_message_id,
    )
    return "dispatched"


async def _mark_failed(
    item: ApprovalItem, reason: str, now: _dt.datetime,
) -> str:
    """Transition the item to ``dispatch_failed`` with the reason
    recorded in decision_notes (prefixed so it doesn't collide with
    principal-written notes)."""
    item.status = "dispatch_failed"
    item.updated_at = now
    item.decision_notes = f"[dispatch] {reason}"
    return reason


async def _create_outlook_draft(
    ctx: GraphContext,
    *,
    payload: dict[str, Any],
    body_html: str,
) -> FullEmailMessage:
    return await create_draft(
        ctx,
        to=list(payload["to"]),
        subject=str(payload["subject"]),
        body=body_html,
        body_content_type="html",
        cc=payload.get("cc"),
        bcc=payload.get("bcc"),
        in_reply_to=payload.get("in_reply_to_message_id"),
    )


async def sweep_dispatch(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    now: _dt.datetime | None = None,
    firm_ids: list[uuid.UUID] | None = None,
) -> DispatchResult:
    """Run one pass of the platform-wide dispatch sweep.

    Walks every active firm's ``approval_items`` with
    ``status IN ('approved', 'dispatch_failed')`` and category
    ``'email_draft'``. Per item: build per-user GraphContext,
    create the Outlook draft, transition to ``sent`` (or
    ``dispatch_failed`` on retryable error).

    Args:
        sessionmaker: shared async sessionmaker.
        now: injectable clock.
        firm_ids: optional explicit list. ``None`` (production)
            triggers ``list_active_firm_ids`` discovery; tests
            pass a list so a shared DB doesn't leak in other
            tests' firms.
    """
    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    result = DispatchResult()

    if firm_ids is None:
        async with sessionmaker() as session:
            firm_ids = await list_active_firm_ids(session)

    result.firms_seen = len(firm_ids)
    logger.info("dispatch sweep firms={}", len(firm_ids))

    for firm_id in firm_ids:
        await _sweep_firm(
            firm_id=firm_id,
            sessionmaker=sessionmaker,
            now=now,
            result=result,
        )

    logger.info(
        "dispatch sweep done firms={} items={} sent={} failed={} actions={}",
        result.firms_seen,
        result.items_seen,
        result.dispatched,
        result.failed,
        result.actions,
    )
    return result


async def _sweep_firm(
    *,
    firm_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    now: _dt.datetime,
    result: DispatchResult,
) -> None:
    async with sessionmaker() as session, firm_context(firm_id):
        items = (
            await session.execute(
                select(ApprovalItem)
                .where(ApprovalItem.firm_id == firm_id)
                .where(
                    ApprovalItem.status.in_(
                        ("approved", "dispatch_failed")
                    )
                )
                .where(ApprovalItem.category == "email_draft")
                .order_by(ApprovalItem.updated_at.asc())
            )
        ).scalars().all()

        if not items:
            return

        for item in items:
            result.items_seen += 1
            action = await dispatch_email_draft(
                session=session, item=item, now=now,
            )
            result.record(action)
            if action == "dispatched":
                result.dispatched += 1
            else:
                result.failed += 1

        await session.commit()
