"""Delivery-status side helpers (pre-pilot Task 3).

Operations against ``approval_items.delivery_status`` and the
companion ``delivery_status_detail`` / ``delivery_status_updated_at``
/ ``executed_internet_message_id`` columns. Kept separate from
``approval.items`` so the approval-side state machine
(pending/approved/rejected/sent/dispatch_failed) doesn't grow a
second axis of concerns.

Two operations land here:

- ``mark_delivery_failed`` — flips ``delivery_status='failed'`` for
  one row matched by ``executed_internet_message_id``. Used by the
  ``delivery_status_handler`` plugin when it correlates an
  incoming NDR back to the row that produced it. The match must
  be exact; the lookup is scoped to the current firm by RLS.

- ``sweep_delivery_confirmation`` — walks every firm's
  ``delivery_status='sent'`` rows older than the configured
  confirmation window (default 4h) and flips them to
  ``'delivered'``. Microsoft would have NDR'd within the window
  if the send had failed; surviving past it is a positive signal.
"""
import datetime as _dt
import uuid
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.db.firms import list_active_firm_ids
from coworker.db.models import ApprovalItem
from coworker.db.session import firm_context

# 4h matches the brief's ``NDR_DELIVERY_WINDOW_SECONDS=14400``.
# Microsoft's bounce window is typically well under 4h; surviving
# past it is the signal we use for ``delivered``.
DELIVERY_CONFIRMATION_WINDOW = _dt.timedelta(hours=4)


@dataclass
class DeliveryFailedResult:
    """Outcome of one ``mark_delivery_failed`` call.

    ``correlated`` is False when no row matched the supplied
    Internet Message ID — typically because OWA regenerated the
    Message-ID at send time (documented carry-forward). The caller
    logs the miss at WARN; the NDR itself stays unactioned.
    """

    correlated: bool
    approval_item_id: uuid.UUID | None = None


async def mark_delivery_failed(
    session: AsyncSession,
    *,
    internet_message_id: str,
    detail: str,
    now: _dt.datetime | None = None,
) -> DeliveryFailedResult:
    """Flip delivery_status='failed' for the row whose
    executed_internet_message_id matches.

    Args:
        session: AsyncSession already inside ``firm_context``.
        internet_message_id: the RFC 5322 Message-ID extracted
            from the NDR's In-Reply-To / References header. Must
            include angle brackets if Graph's stored value does;
            the comparison is exact.
        detail: free-text reason — typically the NDR's
            ``Diagnostic-Code`` header or a short failure summary
            ("550 5.1.1 user unknown"). Truncated to 500 chars by
            the caller.
        now: injectable clock; defaults to UTC now.

    Returns:
        ``DeliveryFailedResult`` with correlated=False when no
        row matched (the message-id is logged at WARN by the
        caller, not here).
    """
    if not internet_message_id:
        raise ValueError("internet_message_id must be non-empty")

    now = now if now is not None else _dt.datetime.now(_dt.UTC)

    item = (
        await session.execute(
            select(ApprovalItem).where(
                ApprovalItem.executed_internet_message_id
                == internet_message_id
            )
        )
    ).scalar_one_or_none()

    if item is None:
        return DeliveryFailedResult(correlated=False)

    item.delivery_status = "failed"
    item.delivery_status_detail = detail[:500]
    item.delivery_status_updated_at = now
    item.updated_at = now
    await session.flush()
    logger.info(
        "delivery_status=failed item_id={} internet_message_id={!r} "
        "detail={!r}",
        item.id, internet_message_id, detail[:120],
    )
    return DeliveryFailedResult(
        correlated=True, approval_item_id=item.id,
    )


@dataclass
class DeliveryConfirmResult:
    """Counters for one ``sweep_delivery_confirmation`` pass."""

    firms_seen: int = 0
    items_seen: int = 0
    confirmed: int = 0
    skipped_no_internet_id: int = 0
    actions: dict[str, int] = field(default_factory=dict)


async def sweep_delivery_confirmation(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    now: _dt.datetime | None = None,
    firm_ids: list[uuid.UUID] | None = None,
    window: _dt.timedelta = DELIVERY_CONFIRMATION_WINDOW,
) -> DeliveryConfirmResult:
    """Flip ``delivery_status='sent'`` rows past the window to
    ``'delivered'``.

    Args:
        sessionmaker: shared async sessionmaker.
        now: injectable clock.
        firm_ids: optional explicit firm list (tests pin it).
            ``None`` triggers ``list_active_firm_ids`` discovery.
        window: the confirmation window. Defaults to 4h.

    Returns:
        A ``DeliveryConfirmResult`` with counts. The sweep is
        idempotent — running it twice produces no further
        transitions, because rows already flipped are no longer
        in 'sent'.
    """
    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    result = DeliveryConfirmResult()
    cutoff = now - window

    if firm_ids is None:
        async with sessionmaker() as session:
            firm_ids = await list_active_firm_ids(session)

    result.firms_seen = len(firm_ids)
    logger.info(
        "delivery confirm sweep firms={} cutoff={}",
        len(firm_ids), cutoff.isoformat(),
    )

    for firm_id in firm_ids:
        await _sweep_firm_delivery(
            firm_id=firm_id,
            sessionmaker=sessionmaker,
            now=now,
            cutoff=cutoff,
            result=result,
        )

    logger.info(
        "delivery confirm sweep done firms={} items={} confirmed={} "
        "skipped_no_internet_id={}",
        result.firms_seen, result.items_seen, result.confirmed,
        result.skipped_no_internet_id,
    )
    return result


async def _sweep_firm_delivery(
    *,
    firm_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    now: _dt.datetime,
    cutoff: _dt.datetime,
    result: DeliveryConfirmResult,
) -> None:
    async with sessionmaker() as session, firm_context(firm_id):
        items = (
            await session.execute(
                select(ApprovalItem)
                .where(ApprovalItem.firm_id == firm_id)
                .where(ApprovalItem.delivery_status == "sent")
                .where(
                    ApprovalItem.delivery_status_updated_at < cutoff
                )
                .order_by(
                    ApprovalItem.delivery_status_updated_at.asc()
                )
            )
        ).scalars().all()

        if not items:
            return

        for item in items:
            result.items_seen += 1
            # Surface but don't block confirmations when we never
            # captured a Message-ID — OWA-regenerated drafts land
            # here; their NDRs (if any) would never have
            # correlated, so flipping to 'delivered' is the right
            # default. Counter is informational.
            if item.executed_internet_message_id is None:
                result.skipped_no_internet_id += 1
            item.delivery_status = "delivered"
            item.delivery_status_updated_at = now
            item.updated_at = now
            result.confirmed += 1

        await session.commit()
