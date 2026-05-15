"""Integration tests for ``sweep_scheduler``.

Walks every active firm's enabled scheduled plugins and enqueues
``scheduled`` PluginEvents for the cron ticks that have elapsed
since last_fired_at. Redis is the test instance; the registry is
populated with two stub plugins (one scheduled, one not) so we
exercise the discriminator.
"""
import datetime as _dt
import json
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import pytest_asyncio
from redis.asyncio import from_url
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.config import get_settings
from coworker.db.models import Firm, PluginInstallation
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.plugins.base import (
    OrchestratorPlugin,
    PluginRegistry,
    PluginRun,
)
from coworker.plugins.scheduler import sweep_scheduler
from coworker.workers.plugin_queue import PluginEventQueue

_TEST_REDIS_DB = "/9"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


def _fresh_test_redis():
    return from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )


# ---------------------------------------------------------------------------
# Stub plugins
# ---------------------------------------------------------------------------


class _HourlyPlugin(OrchestratorPlugin):
    name = "hourly_test"
    display_name = "Hourly Test"
    description = "Fires at the top of every hour."
    triggers = frozenset({"scheduled", "manual"})
    schedule_cron = "0 * * * *"
    enabled_tool_categories = frozenset({"reasoning"})

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        return "tick"


class _NonScheduledPlugin(OrchestratorPlugin):
    name = "email_only"
    display_name = "Email Only"
    description = "No schedule_cron."
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"reasoning"})

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        return "respond"


def _registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.register(_HourlyPlugin)
    reg.register(_NonScheduledPlugin)
    return reg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sched_env(test_database_url) -> AsyncIterator[dict]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    redis = _fresh_test_redis()
    await redis.flushdb()
    queue = PluginEventQueue(redis)
    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "queue": queue, "redis": redis, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await redis.flushdb()
        await redis.aclose()
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = ("firms", "users", "audit_log", "plugin_installations")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in ("plugin_installations", "audit_log", "users"):
                await session.execute(
                    text(f"DELETE FROM {t} WHERE firm_id = :id"),
                    {"id": str(firm_id)},
                )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"),
                {"id": str(firm_id)},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


async def _seed_firm(sm) -> tuple[uuid.UUID, str]:
    firm_id = uuid.uuid4()
    slug = f"sched-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Sched Firm", slug=slug))
        await session.commit()
    return firm_id, slug


async def _install(
    sm, firm_id, plugin_name, *, last_fired_at=None, enabled=True,
) -> uuid.UUID:
    async with sm() as session, firm_context(firm_id):
        row = PluginInstallation(
            firm_id=firm_id,
            plugin_name=plugin_name,
            plugin_version="0.1.0",
            is_enabled=enabled,
            is_dry_run=False,
            config={},
            installed_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
            last_fired_at=last_fired_at,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _queue_size(redis) -> int:
    return await redis.llen("queue:plugin_events")


# ===========================================================================
# Tests
# ===========================================================================


async def test_due_cron_fires_event(sched_env) -> None:
    sm = sched_env["sm"]
    firm_id, slug = await _seed_firm(sm)
    sched_env["created"].append(firm_id)
    install_id = await _install(
        sm, firm_id, _HourlyPlugin.name,
        last_fired_at=_dt.datetime(2026, 5, 15, 9, 0, tzinfo=_dt.UTC),
    )

    # now > next cron tick (10:00) -> should fire
    now = _dt.datetime(2026, 5, 15, 10, 5, tzinfo=_dt.UTC)
    result = await sweep_scheduler(
        sessionmaker=sm,
        queue=sched_env["queue"],
        plugin_registry=_registry(),
        now=now,
        firm_ids=[firm_id],
    )

    assert result.fired == 1
    assert result.actions == {"fired": 1}
    assert await _queue_size(sched_env["redis"]) == 1

    raw = await sched_env["redis"].lrange("queue:plugin_events", 0, -1)
    event = json.loads(raw[0])
    assert event["trigger"] == "scheduled"
    assert event["firm_slug"] == slug
    assert event["firm_id"] == str(firm_id)
    assert event["event_data"]["plugin_name"] == "hourly_test"
    assert event["event_data"]["schedule_cron"] == "0 * * * *"
    assert event["event_data"]["scheduled_at"].startswith("2026-05-15T10:00")

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(PluginInstallation)
                .where(PluginInstallation.id == install_id)
            )
        ).scalar_one()
        # last_fired_at advanced to the cron tick time.
        assert row.last_fired_at == _dt.datetime(
            2026, 5, 15, 10, 0, tzinfo=_dt.UTC,
        )


async def test_not_yet_due_does_not_fire(sched_env) -> None:
    sm = sched_env["sm"]
    firm_id, _ = await _seed_firm(sm)
    sched_env["created"].append(firm_id)
    await _install(
        sm, firm_id, _HourlyPlugin.name,
        last_fired_at=_dt.datetime(2026, 5, 15, 10, 0, tzinfo=_dt.UTC),
    )

    # now < 11:00 (next tick) -> should not fire
    now = _dt.datetime(2026, 5, 15, 10, 30, tzinfo=_dt.UTC)
    result = await sweep_scheduler(
        sessionmaker=sm,
        queue=sched_env["queue"],
        plugin_registry=_registry(),
        now=now,
        firm_ids=[firm_id],
    )

    assert result.fired == 0
    assert result.actions == {"not_due": 1}


async def test_non_scheduled_plugin_skipped(sched_env) -> None:
    """An email-only plugin without schedule_cron is skipped."""
    sm = sched_env["sm"]
    firm_id, _ = await _seed_firm(sm)
    sched_env["created"].append(firm_id)
    await _install(sm, firm_id, _NonScheduledPlugin.name)

    result = await sweep_scheduler(
        sessionmaker=sm,
        queue=sched_env["queue"],
        plugin_registry=_registry(),
        now=_dt.datetime(2026, 5, 15, 10, 0, tzinfo=_dt.UTC),
        firm_ids=[firm_id],
    )

    assert result.fired == 0
    assert result.actions == {"no_cron": 1}


async def test_disabled_installation_skipped(sched_env) -> None:
    sm = sched_env["sm"]
    firm_id, _ = await _seed_firm(sm)
    sched_env["created"].append(firm_id)
    await _install(
        sm, firm_id, _HourlyPlugin.name,
        last_fired_at=None, enabled=False,
    )

    result = await sweep_scheduler(
        sessionmaker=sm,
        queue=sched_env["queue"],
        plugin_registry=_registry(),
        now=_dt.datetime(2026, 5, 15, 10, 0, tzinfo=_dt.UTC),
        firm_ids=[firm_id],
    )

    # The row is filtered out by the is_enabled=TRUE clause; the
    # scheduler doesn't even look at it.
    assert result.installations_seen == 0
    assert result.fired == 0


async def test_catch_up_fires_one_event_for_most_recent_missed_tick(
    sched_env,
) -> None:
    """If the timer was paused across many ticks, the next sweep
    fires ONE event for the most recent eligible tick (not N events,
    which would flood the queue)."""
    sm = sched_env["sm"]
    firm_id, _ = await _seed_firm(sm)
    sched_env["created"].append(firm_id)
    await _install(
        sm, firm_id, _HourlyPlugin.name,
        last_fired_at=_dt.datetime(2026, 5, 15, 5, 0, tzinfo=_dt.UTC),
    )

    # 4-hour gap; ticks at 06, 07, 08, 09 all missed.
    now = _dt.datetime(2026, 5, 15, 9, 30, tzinfo=_dt.UTC)
    result = await sweep_scheduler(
        sessionmaker=sm,
        queue=sched_env["queue"],
        plugin_registry=_registry(),
        now=now,
        firm_ids=[firm_id],
    )

    # APScheduler's get_next_fire_time(previous, now) returns the
    # NEXT tick after previous (06:00). Only one event enqueues per
    # sweep call; further ticks fire on subsequent sweep passes.
    assert result.fired == 1
    assert await _queue_size(sched_env["redis"]) == 1


async def test_first_fire_uses_installed_at(sched_env) -> None:
    """A freshly-installed plugin's first firing is bounded by
    installed_at, not "any past tick"."""
    sm = sched_env["sm"]
    firm_id, _ = await _seed_firm(sm)
    sched_env["created"].append(firm_id)
    # installed_at defaults to 2026-01-01 from the helper; with
    # an hourly cron and now = 2026-05-15 10:30, the first cron
    # tick after installed_at is 2026-01-01 01:00 — i.e. fire.
    await _install(sm, firm_id, _HourlyPlugin.name, last_fired_at=None)

    now = _dt.datetime(2026, 5, 15, 10, 30, tzinfo=_dt.UTC)
    result = await sweep_scheduler(
        sessionmaker=sm,
        queue=sched_env["queue"],
        plugin_registry=_registry(),
        now=now,
        firm_ids=[firm_id],
    )

    assert result.fired == 1


async def test_unknown_plugin_in_registry_logs(sched_env) -> None:
    """An installation row for a plugin name the registry doesn't
    know about (e.g. plugin uninstalled from code) is skipped."""
    sm = sched_env["sm"]
    firm_id, _ = await _seed_firm(sm)
    sched_env["created"].append(firm_id)
    await _install(sm, firm_id, "ghost_plugin_never_registered")

    result = await sweep_scheduler(
        sessionmaker=sm,
        queue=sched_env["queue"],
        plugin_registry=_registry(),
        now=_dt.datetime(2026, 5, 15, 10, 0, tzinfo=_dt.UTC),
        firm_ids=[firm_id],
    )

    assert result.fired == 0
    assert result.actions == {"not_in_registry": 1}
