"""Platform-wide subscription sweep.

Iterates every active firm and every active-processor user, and
calls ``ensure_subscription`` so each user's inbox has a fresh
Graph change-notification subscription. The systemd timer
(``infra/systemd/coworker-subscribe.timer``) runs this on a
cadence shorter than ``DEFAULT_RENEWAL_BUFFER`` so renewals never
fall behind.

Errors are isolated per-user: one missing Azure cred or one
Graph 429 doesn't abort the sweep. The function returns a
summary so the caller can log and / or surface to ops.
"""
import datetime as _dt
import uuid
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.connectors.exceptions import ConnectorError
from coworker.db.firms import list_active_firm_ids
from coworker.db.models import Firm, User
from coworker.db.session import firm_context
from coworker.graph.subscription_bootstrap import (
    INBOX_MESSAGES_RESOURCE_TEMPLATE,
    ensure_subscription,
)
from coworker.graph.subscriptions import AppGraphContext, graph_app_context


@dataclass
class SweepResult:
    """Per-sweep summary.

    ``actions`` maps action name (``"reused"`` / ``"renewed"`` /
    ``"created"``) -> count. ``firm_errors`` is one entry per
    firm-level failure (e.g. missing Azure creds); ``user_errors``
    is one entry per user-level failure (e.g. a single bad
    mailbox). Counts let dashboards alert on regressions
    independent of per-firm logging volume.
    """

    firms_seen: int = 0
    users_seen: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    firm_errors: list[str] = field(default_factory=list)
    user_errors: list[str] = field(default_factory=list)

    def record_action(self, action: str) -> None:
        self.actions[action] = self.actions.get(action, 0) + 1


async def sweep_subscriptions(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    public_webhook_base_url: str,
    now: _dt.datetime | None = None,
    firm_ids: list[uuid.UUID] | None = None,
) -> SweepResult:
    """Run one pass of the platform-wide subscription sweep.

    Args:
        sessionmaker: shared async sessionmaker. The sweep opens
            one session for the cross-firm firm-id listing and
            then one per firm for the per-user work.
        public_webhook_base_url: e.g.
            ``https://coworker.mcands.com.au``. The sweep appends
            ``/webhooks/graph/{firm_slug}``. Must be HTTPS in
            production — Microsoft rejects non-HTTPS notification
            URLs.
        now: injectable clock; defaults to UTC now.
        firm_ids: optional override. When None (production), the
            sweep calls ``list_active_firm_ids`` to discover every
            active firm. Tests pass an explicit list so a shared
            DB doesn't leak in other tests' firms.

    Returns:
        ``SweepResult`` summarising firms and users visited plus
        any per-scope errors.
    """
    if not public_webhook_base_url:
        raise ValueError(
            "public_webhook_base_url must be non-empty; set "
            "PUBLIC_WEBHOOK_BASE_URL in the worker's env"
        )

    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    result = SweepResult()

    if firm_ids is None:
        async with sessionmaker() as session:
            firm_ids = await list_active_firm_ids(session)

    logger.info("subscription sweep firms={}", len(firm_ids))
    result.firms_seen = len(firm_ids)

    for firm_id in firm_ids:
        await _sweep_firm(
            firm_id=firm_id,
            sessionmaker=sessionmaker,
            public_webhook_base_url=public_webhook_base_url,
            now=now,
            result=result,
        )

    logger.info(
        "subscription sweep done firms={} users={} actions={} "
        "firm_errors={} user_errors={}",
        result.firms_seen,
        result.users_seen,
        result.actions,
        len(result.firm_errors),
        len(result.user_errors),
    )
    return result


async def _sweep_firm(
    *,
    firm_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    public_webhook_base_url: str,
    now: _dt.datetime,
    result: SweepResult,
) -> None:
    """Run the sweep for one firm."""
    async with sessionmaker() as session, firm_context(firm_id):
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one_or_none()
        if firm is None:
            logger.warning(
                "subscription sweep firm vanished firm_id={}", firm_id,
            )
            return

        users = (
            await session.execute(
                select(User)
                .where(User.firm_id == firm_id)
                .where(User.is_active_processor.is_(True))
            )
        ).scalars().all()

        if not users:
            logger.debug(
                "subscription sweep no active users firm_id={}", firm_id,
            )
            return

        try:
            ctx = await graph_app_context(session, firm)
        except (ValueError, ConnectorError) as exc:
            # Missing Azure creds / login endpoint failure: skip the
            # whole firm, record the error, move on.
            logger.warning(
                "subscription sweep firm graph_app_context failed "
                "firm_id={} err={}",
                firm_id, exc,
            )
            result.firm_errors.append(f"{firm_id}: {type(exc).__name__}: {exc}")
            return

        notification_url = (
            f"{public_webhook_base_url.rstrip('/')}"
            f"/webhooks/graph/{firm.slug}"
        )

        for user in users:
            await _sweep_user(
                session=session,
                ctx=ctx,
                user=user,
                notification_url=notification_url,
                now=now,
                result=result,
            )

        await session.commit()


async def _sweep_user(
    *,
    session: AsyncSession,
    ctx: AppGraphContext,
    user: User,
    notification_url: str,
    now: _dt.datetime,
    result: SweepResult,
) -> None:
    """Run ensure_subscription for one user's inbox."""
    result.users_seen += 1
    resource = INBOX_MESSAGES_RESOURCE_TEMPLATE.format(
        azure_object_id=user.azure_object_id,
    )
    try:
        outcome = await ensure_subscription(
            session=session,
            ctx=ctx,
            user=user,
            resource=resource,
            notification_url=notification_url,
            now=now,
        )
    except ConnectorError as exc:
        logger.warning(
            "subscription sweep user failed user_id={} err={}",
            user.id, exc,
        )
        result.user_errors.append(
            f"{user.id}: {type(exc).__name__}: {exc}"
        )
        return
    except Exception as exc:
        logger.exception(
            "subscription sweep user unexpected error user_id={}",
            user.id,
        )
        result.user_errors.append(
            f"{user.id}: {type(exc).__name__}: {exc}"
        )
        return

    logger.info(
        "subscription sweep user_id={} action={} sub_id={}",
        user.id, outcome.action, outcome.row.subscription_id,
    )
    result.record_action(outcome.action)
