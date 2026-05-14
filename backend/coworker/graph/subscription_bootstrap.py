"""Idempotent bootstrap: create or renew a Graph subscription row.

Called from the Phase 11-3 scheduler job once per (user, resource)
pair the platform wants to monitor. Encapsulates three decisions:

1. **Do we already have an active row?** If yes and it's not near
   expiry, no-op — the existing subscription is still valid.
2. **Is the existing row near expiry?** Renew via Graph PATCH; on
   success, update ``expiration_date_time`` + ``last_renewed_at``.
   If Graph returns 404 (sub was deleted out of band), fall
   through to fresh creation.
3. **Fresh subscription:** generate a new client_state, create the
   subscription via Graph POST, encrypt the client_state with
   firm-AAD, and persist a new row (or replace the stale one).

The caller commits the session — this function flushes but does
not commit, so caller controls transactional scope.
"""
import datetime as _dt
import secrets
import uuid
from collections.abc import Callable
from typing import NamedTuple

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import ConnectorNotFound
from coworker.db.models import Firm, GraphSubscription, User
from coworker.graph.subscriptions import (
    AppGraphContext,
    Subscription,
    renew_subscription,
    subscribe_change_notifications,
)
from coworker.security.encryption import encrypt_str

# Microsoft caps message subscriptions at ~4230 minutes (just over
# 70 hours); we ask for 2 days 22 hours which leaves headroom for
# clock skew and still beats the cap.
DEFAULT_SUBSCRIPTION_TTL = _dt.timedelta(days=2, hours=22)

# When the existing row's expiration is within this window, the
# bootstrap renews proactively rather than waiting for actual expiry.
DEFAULT_RENEWAL_BUFFER = _dt.timedelta(hours=12)

# Default Graph resource path for an Outlook mailbox's inbox.
INBOX_MESSAGES_RESOURCE_TEMPLATE = (
    "users/{azure_object_id}/mailFolders('Inbox')/messages"
)


class EnsureOutcome(NamedTuple):
    """Result of ``ensure_subscription``.

    ``action`` is one of: ``"reused"`` (existing row still valid),
    ``"renewed"`` (existing row's expiration extended via Graph
    PATCH), ``"created"`` (new Graph subscription created and
    persisted, optionally replacing a stale row).
    """

    row: GraphSubscription
    action: str


async def ensure_subscription(
    *,
    session: AsyncSession,
    ctx: AppGraphContext,
    user: User,
    resource: str,
    notification_url: str,
    now: _dt.datetime | None = None,
    ttl: _dt.timedelta = DEFAULT_SUBSCRIPTION_TTL,
    renewal_buffer: _dt.timedelta = DEFAULT_RENEWAL_BUFFER,
    change_type: str = "created,updated",
    client_state_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
) -> EnsureOutcome:
    """Create-or-renew a subscription for ``(firm, user, resource)``.

    Args:
        session: AsyncSession within firm_context(firm.id). Caller
            owns commit; we flush so subsequent reads see our row.
        ctx: app-only Graph context for the firm. Caller has already
            handled token caching via ``graph_app_context``.
        user: the mailbox-owner User row whose resource we monitor.
            Must belong to ``ctx.firm`` (assert checked).
        resource: Graph resource path. For an Outlook inbox use
            ``INBOX_MESSAGES_RESOURCE_TEMPLATE.format(azure_object_id=...)``.
        notification_url: where Graph will POST. Public origin +
            ``/webhooks/graph/{firm_slug}``.
        now: injection point for testing; defaults to UTC now.
        ttl: requested subscription lifetime. Capped by Microsoft;
            see ``DEFAULT_SUBSCRIPTION_TTL``.
        renewal_buffer: if an existing row expires within this
            window, renew it.
        change_type: which mutations to subscribe to.
        client_state_factory: zero-arg callable returning the secret
            string to use as ``clientState``. Defaults to a fresh
            URL-safe 32-byte token. Injectable so tests can pin
            the value.

    Returns:
        ``EnsureOutcome`` with the persisted row and the action
        that was taken (``reused`` / ``renewed`` / ``created``).
    """
    if user.firm_id != ctx.firm.id:
        raise ValueError(
            f"user.firm_id {user.firm_id} does not match ctx.firm.id "
            f"{ctx.firm.id}"
        )

    now = now if now is not None else _dt.datetime.now(_dt.UTC)

    existing = await _load_existing(
        session, firm_id=ctx.firm.id, user_id=user.id, resource=resource,
    )

    if existing is not None:
        if existing.expiration_date_time > now + renewal_buffer:
            return EnsureOutcome(row=existing, action="reused")
        # Near-expiry — renew via Graph.
        try:
            renewed = await renew_subscription(
                ctx,
                existing.subscription_id,
                expiration_date_time=now + ttl,
            )
        except ConnectorNotFound:
            logger.warning(
                "subscription renewal 404 — sub deleted out of band; "
                "creating fresh sub_id={} user_id={}",
                existing.subscription_id,
                user.id,
            )
            await session.delete(existing)
            await session.flush()
        else:
            existing.expiration_date_time = renewed.expiration_date_time
            existing.last_renewed_at = now
            existing.updated_at = now
            await session.flush()
            return EnsureOutcome(row=existing, action="renewed")

    # Fresh creation (either no row, or we deleted a stale one).
    client_state = client_state_factory()
    created = await subscribe_change_notifications(
        ctx,
        resource=resource,
        notification_url=notification_url,
        expiration_date_time=now + ttl,
        client_state=client_state,
        change_type=change_type,
        # Lifecycle events (subscriptionRemoved /
        # reauthorizationRequired / missed) come back to the same
        # webhook URL; the handler discriminates internally.
        lifecycle_notification_url=notification_url,
    )
    row = _row_from_subscription(
        firm=ctx.firm,
        user=user,
        created=created,
        client_state=client_state,
        change_type=change_type,
        now=now,
    )
    session.add(row)
    await session.flush()
    return EnsureOutcome(row=row, action="created")


async def _load_existing(
    session: AsyncSession,
    *,
    firm_id: uuid.UUID,
    user_id: uuid.UUID,
    resource: str,
) -> GraphSubscription | None:
    return (
        await session.execute(
            select(GraphSubscription)
            .where(GraphSubscription.firm_id == firm_id)
            .where(GraphSubscription.user_id == user_id)
            .where(GraphSubscription.resource == resource)
        )
    ).scalar_one_or_none()


def _row_from_subscription(
    *,
    firm: Firm,
    user: User,
    created: Subscription,
    client_state: str,
    change_type: str,
    now: _dt.datetime,
) -> GraphSubscription:
    return GraphSubscription(
        firm_id=firm.id,
        user_id=user.id,
        subscription_id=created.id,
        resource=created.resource,
        notification_url=created.notification_url,
        change_type=change_type,
        client_state_ciphertext=encrypt_str(
            client_state, firm_id=str(firm.id)
        ),
        expiration_date_time=created.expiration_date_time,
        created_at=now,
        updated_at=now,
    )
