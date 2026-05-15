"""Integration tests for ``PluginRunDedup`` and the processor's
dedup wiring.

Real DB + the test Redis instance (logical DB 9). We exercise
both the helper directly (key derivation, claim semantics, TTL)
and the integrated path through ``process_event``.
"""
import datetime as _dt
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import pytest_asyncio
from pydantic import BaseModel
from redis.asyncio import from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.config import get_settings
from coworker.db.models import Firm, PluginInstallation
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.orchestrator.engine import ModelCallResult
from coworker.orchestrator.tools import ToolRegistry
from coworker.plugins.base import OrchestratorPlugin, PluginRegistry, PluginRun
from coworker.workers.dedup import (
    DEFAULT_TTL,
    PluginRunDedup,
    derive_dedup_key,
)
from coworker.workers.plugin_queue import PluginEvent
from coworker.workers.processor import process_event

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
# Stub plugin for the integrated tests
# ---------------------------------------------------------------------------


class _EmailPlugin(OrchestratorPlugin):
    name = "dedup_test_email"
    display_name = "Dedup Test Email"
    description = "stub"
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"reasoning"})

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        return "noop"


class ScriptedCaller:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_) -> ModelCallResult:  # type: ignore[no-untyped-def]
        self.calls += 1
        return ModelCallResult(
            content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            input_tokens=1, output_tokens=1,
            model="claude-sonnet-4-6",
        )


class _StubAnthropic(BaseModel):
    pass


def _stub_anthropic_factory(firm):
    return _StubAnthropic()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dedup_env(test_database_url) -> AsyncIterator[dict]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    redis = _fresh_test_redis()
    await redis.flushdb()
    dedup = PluginRunDedup(redis)
    created: list[uuid.UUID] = []
    try:
        yield {
            "sm": sm, "redis": redis, "dedup": dedup, "created": created,
        }
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await redis.flushdb()
        await redis.aclose()
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = (
        "firms", "users", "audit_log", "plugin_installations",
        "agent_traces", "agent_trace_steps",
    )
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in (
                "agent_trace_steps", "agent_traces", "plugin_installations",
                "audit_log", "users",
            ):
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


async def _seed_firm_and_install(sm) -> uuid.UUID:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(
            id=firm_id, name="Dedup Firm",
            slug=f"d-{uuid.uuid4().hex[:8]}",
        ))
        await session.flush()
        session.add(PluginInstallation(
            firm_id=firm_id, plugin_name=_EmailPlugin.name,
            plugin_version="0.1.0", is_enabled=True, is_dry_run=False,
            config={},
        ))
        await session.commit()
    return firm_id


def _event(*, firm_id, message_id="msg-1", trigger="email_received") -> PluginEvent:
    return PluginEvent(
        event_id=uuid.uuid4(), trigger=trigger,
        firm_slug="x", firm_id=firm_id,
        event_data={
            "message_id": message_id,
            "change_type": "created",
            "resource": f"users/oid/messages/{message_id}",
        },
        enqueued_at=_dt.datetime.now(_dt.UTC),
    )


def _registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.register(_EmailPlugin)
    return reg


# ===========================================================================
# Key derivation
# ===========================================================================


def test_derive_email_key() -> None:
    e = _event(firm_id=uuid.uuid4(), message_id="abc-123")
    assert derive_dedup_key(e) == "email:abc-123"


def test_derive_calendar_key() -> None:
    e = _event(
        firm_id=uuid.uuid4(), message_id="ev-77",
        trigger="calendar_event",
    )
    assert derive_dedup_key(e) == "calendar:ev-77"


def test_derive_scheduled_key() -> None:
    e = PluginEvent(
        event_id=uuid.uuid4(), trigger="scheduled",
        firm_slug="x", firm_id=uuid.uuid4(),
        event_data={"scheduled_at": "2026-05-15T07:00:00+00:00"},
        enqueued_at=_dt.datetime.now(_dt.UTC),
    )
    assert derive_dedup_key(e) == "scheduled:2026-05-15T07:00:00+00:00"


def test_derive_returns_none_for_keyless_trigger() -> None:
    e = PluginEvent(
        event_id=uuid.uuid4(), trigger="manual",
        firm_slug="x", firm_id=uuid.uuid4(),
        event_data={},
        enqueued_at=_dt.datetime.now(_dt.UTC),
    )
    assert derive_dedup_key(e) is None


# ===========================================================================
# claim() semantics
# ===========================================================================


async def test_first_claim_succeeds_second_is_blocked(dedup_env) -> None:
    dedup = dedup_env["dedup"]
    firm_id = uuid.uuid4()
    e = _event(firm_id=firm_id, message_id="m-1")

    assert await dedup.claim(firm_id, "smart_responder", e) is True
    assert await dedup.claim(firm_id, "smart_responder", e) is False


async def test_different_plugins_each_get_a_claim(dedup_env) -> None:
    """Per-plugin dedup means fan-out still works — two plugins on
    the same email each get one shot."""
    dedup = dedup_env["dedup"]
    firm_id = uuid.uuid4()
    e = _event(firm_id=firm_id, message_id="m-1")

    assert await dedup.claim(firm_id, "smart_responder", e) is True
    assert await dedup.claim(firm_id, "correspondence_logger", e) is True


async def test_different_firms_each_get_a_claim(dedup_env) -> None:
    dedup = dedup_env["dedup"]
    e_a = _event(firm_id=uuid.uuid4(), message_id="m-1")
    e_b = _event(firm_id=uuid.uuid4(), message_id="m-1")

    assert await dedup.claim(e_a.firm_id, "plugin", e_a) is True
    assert await dedup.claim(e_b.firm_id, "plugin", e_b) is True


async def test_keyless_trigger_always_claims(dedup_env) -> None:
    """``manual`` events have no derivable key — every claim is
    allowed (the caller is expected to fire)."""
    dedup = dedup_env["dedup"]
    firm_id = uuid.uuid4()
    e = PluginEvent(
        event_id=uuid.uuid4(), trigger="manual",
        firm_slug="x", firm_id=firm_id,
        event_data={}, enqueued_at=_dt.datetime.now(_dt.UTC),
    )

    assert await dedup.claim(firm_id, "any", e) is True
    assert await dedup.claim(firm_id, "any", e) is True


async def test_ttl_is_applied(dedup_env) -> None:
    """The claim key has an EX (TTL) so it expires automatically."""
    dedup = dedup_env["dedup"]
    redis = dedup_env["redis"]
    firm_id = uuid.uuid4()
    e = _event(firm_id=firm_id, message_id="m-ttl")

    assert await dedup.claim(firm_id, "plugin", e) is True
    key = f"plugin_dedup:{firm_id}:plugin:email:m-ttl"
    ttl = await redis.ttl(key)
    assert 0 < ttl <= int(DEFAULT_TTL.total_seconds())


# ===========================================================================
# Integrated path through process_event
# ===========================================================================


async def test_process_event_deduped_run_is_skipped(dedup_env) -> None:
    """Second process_event for the same email_received skips the
    plugin run."""
    sm = dedup_env["sm"]
    dedup = dedup_env["dedup"]
    firm_id = await _seed_firm_and_install(sm)
    dedup_env["created"].append(firm_id)

    caller = ScriptedCaller()
    e = _event(firm_id=firm_id, message_id="dedup-m-1")

    first = await process_event(
        e,
        sessionmaker=sm,
        plugin_registry=_registry(),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
        dedup=dedup,
    )
    assert len(first.run_results) == 1
    assert caller.calls == 1

    # Second call with the SAME message_id but a fresh event_id
    # (e.g. a backfill re-enqueue): dedup says "already ran".
    second = await process_event(
        _event(firm_id=firm_id, message_id="dedup-m-1"),
        sessionmaker=sm,
        plugin_registry=_registry(),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
        dedup=dedup,
    )
    assert second.run_results == []
    assert any("deduped" in s for s in second.skipped)
    assert caller.calls == 1  # never called again


async def test_process_event_different_messages_both_run(dedup_env) -> None:
    sm = dedup_env["sm"]
    dedup = dedup_env["dedup"]
    firm_id = await _seed_firm_and_install(sm)
    dedup_env["created"].append(firm_id)

    caller = ScriptedCaller()
    for mid in ("a", "b", "c"):
        await process_event(
            _event(firm_id=firm_id, message_id=mid),
            sessionmaker=sm,
            plugin_registry=_registry(),
            tool_registry=ToolRegistry(),
            model_caller=caller,
            anthropic_factory=_stub_anthropic_factory,
            dedup=dedup,
        )

    assert caller.calls == 3


async def test_process_event_no_dedup_runs_every_time(dedup_env) -> None:
    """When no dedup is wired, repeats still run (the prior
    behaviour). Guards against accidental dependency on dedup."""
    sm = dedup_env["sm"]
    firm_id = await _seed_firm_and_install(sm)
    dedup_env["created"].append(firm_id)

    caller = ScriptedCaller()
    e = _event(firm_id=firm_id, message_id="repeat")

    for _ in range(2):
        await process_event(
            e,
            sessionmaker=sm,
            plugin_registry=_registry(),
            tool_registry=ToolRegistry(),
            model_caller=caller,
            anthropic_factory=_stub_anthropic_factory,
            # dedup omitted — None
        )

    assert caller.calls == 2
