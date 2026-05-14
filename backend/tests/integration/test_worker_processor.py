"""Integration tests for ``coworker.workers.processor.process_event``.

Real DB + ScriptedModelCaller. Per-event fan-out is the unit
under test; the queue / BRPOP loop wrapper that calls it is
covered separately.
"""
import datetime as _dt
import uuid

import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import AgentTrace, Firm, PluginInstallation, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.orchestrator.engine import ModelCallResult
from coworker.orchestrator.tools import ToolRegistry
from coworker.plugins.base import OrchestratorPlugin, PluginRegistry, PluginRun
from coworker.security.encryption import encrypt_str
from coworker.workers.plugin_queue import PluginEvent
from coworker.workers.processor import process_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EmailPlugin(OrchestratorPlugin):
    """Minimal plugin listening to email_received."""

    name = "email_listener"
    display_name = "Email Listener"
    description = "Test plugin"
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"reasoning"})

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        return f"handle {run.event_data.get('message_id', '<no id>')}"


class _ScheduledPlugin(OrchestratorPlugin):
    """Plugin listening to scheduled trigger; shouldn't run on email_received."""

    name = "daily_briefing"
    display_name = "Daily Briefing"
    description = "Test plugin"
    triggers = frozenset({"scheduled"})
    schedule_cron = "0 6 * * *"
    enabled_tool_categories = frozenset({"reasoning"})

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        return "briefing"


class _BrokenConfig(BaseModel):
    must_be_str: str  # missing in test installations -> validation fails


class _BrokenConfigPlugin(OrchestratorPlugin):
    name = "broken_config_plugin"
    display_name = "Broken Config Plugin"
    description = "Test plugin"
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"reasoning"})
    config_schema = _BrokenConfig

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        return "broken"


class _CrashyPlugin(OrchestratorPlugin):
    name = "crashy_plugin"
    display_name = "Crashy Plugin"
    description = "Test plugin"
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"reasoning"})

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        raise RuntimeError("intentional crash in goal()")


class ScriptedCaller:
    """Single response, always succeeds."""

    def __init__(self, text: str = "ok"):
        self._text = text
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        return ModelCallResult(
            content=[{"type": "text", "text": self._text}],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
        )


class _StubAnthropic:
    """Placeholder for the AnthropicClient — handlers don't touch it."""


def _stub_anthropic_factory(firm):
    return _StubAnthropic()  # type: ignore[return-value]


def _event(
    *,
    trigger: str = "email_received",
    firm_slug: str = "test-firm",
    firm_id: uuid.UUID,
    event_data: dict | None = None,
) -> PluginEvent:
    return PluginEvent(
        event_id=uuid.uuid4(),
        trigger=trigger,
        firm_slug=firm_slug,
        firm_id=firm_id,
        event_data=event_data or {
            "message_id": "msg-1",
            "change_type": "created",
            "resource": "users/oid-1/messages/msg-1",
        },
        enqueued_at=_dt.datetime.now(_dt.UTC),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def processor_env(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = (
        "firms", "users", "audit_log", "token_usage",
        "client_interactions", "lessons", "documents",
        "entity_relationships", "entities", "jobs", "deadlines",
        "agent_trace_steps", "agent_traces", "plugin_installations",
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
                "agent_trace_steps", "agent_traces",
                "plugin_installations",
                "entity_relationships", "deadlines", "jobs",
                "documents", "lessons", "client_interactions",
                "entities", "audit_log", "token_usage", "users",
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


async def _seed_firm(sm) -> uuid.UUID:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Proc Firm", slug=f"p-{uuid.uuid4().hex[:8]}"))
        await session.commit()
    return firm_id


async def _seed_user_with_token(sm, firm_id, *, azure_oid: str = "oid-1") -> uuid.UUID:
    firm_id_str = str(firm_id)
    async with sm() as session, firm_context(firm_id):
        user = User(
            firm_id=firm_id,
            azure_object_id=azure_oid,
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Test User",
            ms_access_token_ciphertext=encrypt_str(
                "test-access", firm_id=firm_id_str
            ),
            ms_token_expires_at=_dt.datetime.now(_dt.UTC)
            + _dt.timedelta(hours=1),
        )
        session.add(user)
        await session.commit()
        return user.id


async def _install_plugin(
    sm, firm_id, plugin_name, *, version="0.1.0", config=None,
):
    async with sm() as session, firm_context(firm_id):
        session.add(
            PluginInstallation(
                firm_id=firm_id,
                plugin_name=plugin_name,
                plugin_version=version,
                is_enabled=True,
                is_dry_run=False,
                config=config or {},
            )
        )
        await session.commit()


def _registry(*plugins) -> PluginRegistry:
    reg = PluginRegistry()
    for p in plugins:
        reg.register(p)
    return reg


# ===========================================================================
# Tests
# ===========================================================================


async def test_fans_email_event_to_installed_plugin(processor_env) -> None:
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)
    await _install_plugin(sm, firm_id, _EmailPlugin.name)

    caller = ScriptedCaller("done")
    result = await process_event(
        _event(firm_id=firm_id),
        sessionmaker=sm,
        plugin_registry=_registry(_EmailPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )

    assert result.firm_not_found is False
    assert len(result.run_results) == 1
    assert result.run_results[0].status == "completed"
    assert result.run_results[0].final_text == "done"
    assert result.skipped == []
    assert caller.calls == 1

    # Trace landed.
    async with sm() as session, firm_context(firm_id):
        traces = (
            await session.execute(
                select(AgentTrace).where(AgentTrace.firm_id == firm_id)
            )
        ).scalars().all()
        assert len(traces) == 1
        assert traces[0].plugin_name == "email_listener"


async def test_unmatching_trigger_skips_all_plugins(processor_env) -> None:
    """A scheduled-only plugin doesn't run on an email_received event."""
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)
    await _install_plugin(sm, firm_id, _ScheduledPlugin.name)

    caller = ScriptedCaller()
    result = await process_event(
        _event(firm_id=firm_id, trigger="email_received"),
        sessionmaker=sm,
        plugin_registry=_registry(_ScheduledPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )
    assert result.run_results == []
    assert caller.calls == 0


async def test_plugin_listening_but_not_installed_is_skipped(processor_env) -> None:
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)
    # Plugin in registry but NOT installed for this firm.

    caller = ScriptedCaller()
    result = await process_event(
        _event(firm_id=firm_id),
        sessionmaker=sm,
        plugin_registry=_registry(_EmailPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )
    assert result.run_results == []
    assert any("not_installed" in s for s in result.skipped)
    assert caller.calls == 0


async def test_unknown_firm_returns_firm_not_found(processor_env) -> None:
    sm = processor_env["sm"]
    caller = ScriptedCaller()
    result = await process_event(
        _event(firm_id=uuid.uuid4()),  # not seeded
        sessionmaker=sm,
        plugin_registry=_registry(_EmailPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )
    assert result.firm_not_found is True
    assert result.run_results == []


async def test_multiple_plugins_listening_to_same_trigger_all_run(
    processor_env,
) -> None:
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)

    class _SecondEmailPlugin(_EmailPlugin):
        name = "second_email_listener"

    await _install_plugin(sm, firm_id, _EmailPlugin.name)
    await _install_plugin(sm, firm_id, _SecondEmailPlugin.name)

    caller = ScriptedCaller()
    result = await process_event(
        _event(firm_id=firm_id),
        sessionmaker=sm,
        plugin_registry=_registry(_EmailPlugin, _SecondEmailPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )

    assert len(result.run_results) == 2
    assert caller.calls == 2


async def test_invalid_config_records_skip_not_crash(processor_env) -> None:
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)
    # Install with an empty config — config_schema requires must_be_str.
    await _install_plugin(sm, firm_id, _BrokenConfigPlugin.name)

    caller = ScriptedCaller()
    result = await process_event(
        _event(firm_id=firm_id),
        sessionmaker=sm,
        plugin_registry=_registry(_BrokenConfigPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )
    assert result.run_results == []
    assert any("config_error" in s for s in result.skipped)


async def test_plugin_crash_is_isolated_to_that_plugin(processor_env) -> None:
    """A plugin that crashes in goal() doesn't take down sibling plugins."""
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)
    await _install_plugin(sm, firm_id, _CrashyPlugin.name)
    await _install_plugin(sm, firm_id, _EmailPlugin.name)

    caller = ScriptedCaller()
    result = await process_event(
        _event(firm_id=firm_id),
        sessionmaker=sm,
        plugin_registry=_registry(_CrashyPlugin, _EmailPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )
    # The healthy plugin still ran.
    assert len(result.run_results) == 1
    assert result.run_results[0].status == "completed"
    # The crashy one was skipped.
    assert any("crashed" in s for s in result.skipped)


async def test_graph_ctx_resolved_when_user_matches_resource(
    processor_env,
) -> None:
    """Smoke test: a known azure_oid yields a User; processor walks
    past the resolve step without error.

    The graph_ctx is now wired through execute_plugin (Phase 6-9),
    but this test only sees the run result; the AgentContext-level
    propagation is covered by ``test_plugin_executor`` tests.
    """
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)
    await _seed_user_with_token(sm, firm_id, azure_oid="oid-known")
    await _install_plugin(sm, firm_id, _EmailPlugin.name)

    event = _event(
        firm_id=firm_id,
        event_data={
            "message_id": "msg-1",
            "change_type": "created",
            "resource": "users/oid-known/messages/msg-1",
        },
    )
    caller = ScriptedCaller()
    result = await process_event(
        event,
        sessionmaker=sm,
        plugin_registry=_registry(_EmailPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )
    assert len(result.run_results) == 1


async def test_missing_user_for_resource_logs_but_continues(processor_env) -> None:
    """If the email event refers to a user the firm doesn't have, the
    processor logs but still runs plugins (just without graph_ctx).
    """
    sm = processor_env["sm"]
    firm_id = await _seed_firm(sm)
    processor_env["created"].append(firm_id)
    await _install_plugin(sm, firm_id, _EmailPlugin.name)

    event = _event(
        firm_id=firm_id,
        event_data={
            "message_id": "msg-1",
            "change_type": "created",
            "resource": "users/unknown-oid/messages/msg-1",
        },
    )
    caller = ScriptedCaller()
    result = await process_event(
        event,
        sessionmaker=sm,
        plugin_registry=_registry(_EmailPlugin),
        tool_registry=ToolRegistry(),
        model_caller=caller,
        anthropic_factory=_stub_anthropic_factory,
    )
    # The plugin still ran (with graph_ctx=None).
    assert len(result.run_results) == 1
