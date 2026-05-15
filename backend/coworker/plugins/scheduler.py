"""Scheduled-trigger runner — Phase 12-3.

Pairs with ``coworker-scheduler.timer`` (every 1 minute). Each
tick:

1. List active firms (cross-firm read via the NO FORCE bracket).
2. Per firm, load every enabled plugin_installations row whose
   plugin class has both ``"scheduled"`` in triggers and a
   non-None ``schedule_cron``.
3. For each: parse the cron via APScheduler's CronTrigger and
   ask for the next fire time after ``last_fired_at`` (or
   ``installed_at`` when last_fired_at is NULL — that bounds
   the first firing to "next cron tick after install" rather
   than "any past tick").
4. If that fire time is <= ``now``, enqueue a ``scheduled``
   PluginEvent and stamp ``last_fired_at = next_fire_time``.
   Catch-up: if the system was down across multiple cron ticks
   we fire ONE event for the most recent eligible tick; the
   gap is logged so ops can see it.

The CLI (``coworker.workers.cli_scheduler``) wraps this for the
systemd timer.

Errors are isolated per-installation: a broken cron expression
on plugin X doesn't block plugin Y from firing in the same
firm. The whole firm's commit happens at end-of-firm so partial
progress survives a mid-firm crash.
"""
import datetime as _dt
import uuid
from dataclasses import dataclass, field

from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.db.firms import list_active_firm_ids
from coworker.db.models import Firm, PluginInstallation
from coworker.db.session import firm_context
from coworker.plugins.base import OrchestratorPlugin, PluginRegistry
from coworker.workers.plugin_queue import PluginEventQueue


@dataclass
class SchedulerResult:
    """Per-tick summary.

    ``fired`` is the number of ``scheduled`` events enqueued this
    tick. ``actions`` counts per outcome (``fired`` / ``not_due``
    / ``no_cron`` / ``bad_cron`` / ``not_in_registry``) so
    dashboards can alert independently of total volume.
    """

    firms_seen: int = 0
    installations_seen: int = 0
    fired: int = 0
    actions: dict[str, int] = field(default_factory=dict)

    def record(self, action: str) -> None:
        self.actions[action] = self.actions.get(action, 0) + 1


async def sweep_scheduler(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    queue: PluginEventQueue,
    plugin_registry: PluginRegistry,
    now: _dt.datetime | None = None,
    firm_ids: list[uuid.UUID] | None = None,
) -> SchedulerResult:
    """One pass of the scheduled-trigger sweep.

    Args:
        sessionmaker: shared async sessionmaker.
        queue: shared PluginEventQueue. Synthetic scheduled events
            land here exactly like webhook events.
        plugin_registry: registry of plugin classes. We need it to
            look up each installation's plugin class and read
            ``schedule_cron`` (the cron expression isn't stored in
            the DB — it lives in Python).
        now: injectable clock.
        firm_ids: optional override. ``None`` (production) triggers
            discovery via ``list_active_firm_ids``.

    Returns:
        ``SchedulerResult`` summarising what fired.
    """
    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    result = SchedulerResult()

    if firm_ids is None:
        async with sessionmaker() as session:
            firm_ids = await list_active_firm_ids(session)

    result.firms_seen = len(firm_ids)
    logger.info("scheduler sweep firms={} now={}", len(firm_ids), now)

    for firm_id in firm_ids:
        await _sweep_firm(
            firm_id=firm_id,
            sessionmaker=sessionmaker,
            queue=queue,
            plugin_registry=plugin_registry,
            now=now,
            result=result,
        )

    logger.info(
        "scheduler sweep done firms={} installations={} fired={} actions={}",
        result.firms_seen,
        result.installations_seen,
        result.fired,
        result.actions,
    )
    return result


async def _sweep_firm(
    *,
    firm_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    queue: PluginEventQueue,
    plugin_registry: PluginRegistry,
    now: _dt.datetime,
    result: SchedulerResult,
) -> None:
    async with sessionmaker() as session, firm_context(firm_id):
        firm = (
            await session.execute(
                select(Firm).where(Firm.id == firm_id)
            )
        ).scalar_one_or_none()
        if firm is None:
            return

        installations = (
            await session.execute(
                select(PluginInstallation)
                .where(PluginInstallation.firm_id == firm_id)
                .where(PluginInstallation.is_enabled.is_(True))
            )
        ).scalars().all()

        for installation in installations:
            result.installations_seen += 1
            await _maybe_fire(
                installation=installation,
                firm_slug=firm.slug,
                queue=queue,
                plugin_registry=plugin_registry,
                now=now,
                result=result,
            )

        await session.commit()


async def _maybe_fire(
    *,
    installation: PluginInstallation,
    firm_slug: str,
    queue: PluginEventQueue,
    plugin_registry: PluginRegistry,
    now: _dt.datetime,
    result: SchedulerResult,
) -> None:
    plugin_cls = plugin_registry.get(installation.plugin_name)
    if plugin_cls is None:
        # Installation row exists for a plugin the process doesn't
        # know about — likely a plugin uninstalled from the code
        # but left in the DB. Skip; ops can clean up.
        logger.warning(
            "scheduler unknown plugin firm_id={} plugin={}",
            installation.firm_id, installation.plugin_name,
        )
        result.record("not_in_registry")
        return

    if "scheduled" not in plugin_cls.triggers:
        result.record("no_cron")
        return
    if not plugin_cls.schedule_cron:
        result.record("no_cron")
        return

    try:
        trigger = CronTrigger.from_crontab(
            plugin_cls.schedule_cron, timezone=_dt.UTC,
        )
    except ValueError as exc:
        logger.warning(
            "scheduler bad cron firm_id={} plugin={} cron={!r} err={}",
            installation.firm_id, installation.plugin_name,
            plugin_cls.schedule_cron, exc,
        )
        result.record("bad_cron")
        return

    previous = installation.last_fired_at or installation.installed_at
    next_fire = trigger.get_next_fire_time(previous, now)
    if next_fire is None or next_fire > now:
        result.record("not_due")
        return

    await _fire(
        installation=installation,
        plugin_cls=plugin_cls,
        firm_slug=firm_slug,
        queue=queue,
        scheduled_at=next_fire,
        now=now,
        result=result,
    )


async def _fire(
    *,
    installation: PluginInstallation,
    plugin_cls: type[OrchestratorPlugin],
    firm_slug: str,
    queue: PluginEventQueue,
    scheduled_at: _dt.datetime,
    now: _dt.datetime,
    result: SchedulerResult,
) -> None:
    lag = (now - scheduled_at).total_seconds()
    if lag > 90:
        # The scheduler is supposed to fire within ~60s of the
        # scheduled cron tick. A bigger gap means the timer was
        # paused (system down, container restart) — fire anyway
        # but log so ops can see the catch-up window.
        logger.warning(
            "scheduler catch-up fire firm_id={} plugin={} "
            "scheduled_at={} lag_seconds={}",
            installation.firm_id, installation.plugin_name,
            scheduled_at, int(lag),
        )

    await queue.enqueue(
        trigger="scheduled",
        firm_slug=firm_slug,
        firm_id=installation.firm_id,
        event_data={
            "scheduled_at": scheduled_at.isoformat(),
            "schedule_cron": plugin_cls.schedule_cron,
            "plugin_name": installation.plugin_name,
            "fired_at": now.isoformat(),
        },
    )
    installation.last_fired_at = scheduled_at
    result.fired += 1
    result.record("fired")
    logger.info(
        "scheduler fired firm_id={} plugin={} scheduled_at={}",
        installation.firm_id, installation.plugin_name, scheduled_at,
    )
