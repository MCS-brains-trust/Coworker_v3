"""Backfill for ``lifecycleEvent="missed"`` subscriptions.

When Microsoft tells us a subscription dropped notifications, the
webhook handler sets ``GraphSubscription.last_missed_at``. This
module's ``backfill_missed_for_subscription`` reads that marker,
lists messages received in the catch-up window, re-enqueues each
as a synthetic ``email_received`` PluginEvent, and clears the
marker on success.

The catch-up window starts at the most recent of the row's
``last_renewed_at`` (or ``created_at`` if never renewed) minus a
small grace buffer (5 min) — enough to compensate for clock skew
between the message receivedDateTime and the subscription
timestamps. The window ends at "now".

Duplicate emission is possible: a message that arrived via a
normal notification AND also gets re-enqueued via backfill would
run any matching plugin twice. For the prototype we accept this
— the principal's approval queue (Phase 9) is the de-dup boundary.
"""
import datetime as _dt
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db.models import GraphSubscription
from coworker.graph.context import GraphContext
from coworker.graph.mail import list_inbox
from coworker.workers.plugin_queue import PluginEventQueue

# Buffer subtracted from the "since" timestamp to absorb clock skew
# between subscription timestamps and message receivedDateTime values.
_GRACE = _dt.timedelta(minutes=5)


@dataclass
class BackfillResult:
    """Per-subscription backfill outcome.

    ``enqueued`` is the count of synthetic events posted to the
    queue. ``skipped`` is a brief reason when nothing was done
    (no marker, no catch-up needed, listing failed). Counts let
    dashboards alert on regressions independent of per-row logging.
    """

    subscription_id: str
    enqueued: int = 0
    skipped: str | None = None


async def backfill_missed_for_subscription(
    *,
    session: AsyncSession,
    ctx: GraphContext,
    queue: PluginEventQueue,
    row: GraphSubscription,
    firm_slug: str,
    now: _dt.datetime | None = None,
    top: int = 100,
) -> BackfillResult:
    """Catch up one subscription's dropped notifications.

    Args:
        session: AsyncSession inside ``firm_context(ctx.firm.id)``.
            The caller commits; we flush the row update.
        ctx: per-user GraphContext (delegated token). Built from the
            row's user_id by the caller; we delegate the token-
            refresh decision to ``_resolve_user_access_token``
            upstream.
        queue: the shared plugin event queue. Backfilled events
            land here exactly like normal webhook deliveries.
        row: the subscription that recorded ``last_missed_at``.
        firm_slug: the firm's URL slug — embedded in each synthetic
            PluginEvent so the worker can resolve it the same way
            normal webhook events are resolved.
        now: injectable clock.
        top: cap on the number of messages we'll re-enqueue. Set to
            100 by default so a long missed window doesn't flood
            the queue; future iterations can paginate.

    Returns:
        ``BackfillResult`` summarising the outcome.
    """
    if row.last_missed_at is None:
        return BackfillResult(
            subscription_id=row.subscription_id, skipped="no_missed_marker",
        )

    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    cutoff = (row.last_renewed_at or row.created_at) - _GRACE

    messages = await list_inbox(ctx, top=top, since=cutoff)
    if not messages:
        row.last_missed_at = None
        await session.flush()
        return BackfillResult(
            subscription_id=row.subscription_id, skipped="no_messages",
        )

    enqueued = 0
    for msg in messages:
        await queue.enqueue(
            trigger="email_received",
            firm_slug=firm_slug,
            firm_id=ctx.firm.id,
            event_data={
                "message_id": msg.id,
                "change_type": "created",
                "subscription_id": row.subscription_id,
                "resource": (
                    f"users/{ctx.user.azure_object_id}/messages/{msg.id}"
                ),
                "received_at_webhook": now.isoformat(),
                "backfilled": True,
            },
        )
        enqueued += 1

    # Mark catch-up complete. last_renewed_at is bumped so the next
    # missed event uses a tighter cutoff (avoids re-fetching the same
    # window if Microsoft sends `missed` repeatedly while we're
    # catching up).
    row.last_missed_at = None
    row.last_renewed_at = now
    await session.flush()

    logger.info(
        "subscription backfill sub_id={} firm_slug={} enqueued={}",
        row.subscription_id, firm_slug, enqueued,
    )
    return BackfillResult(
        subscription_id=row.subscription_id, enqueued=enqueued,
    )
