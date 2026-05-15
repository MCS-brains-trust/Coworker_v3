"""Unit tests for the meeting_prep plugin metadata + goal text."""
import datetime as _dt
import uuid

from coworker.plugins.base import PluginRun
from coworker.plugins.builtin.meeting_prep import MeetingPrepPlugin


def _run(config: dict | None = None) -> PluginRun:
    return PluginRun(
        plugin_name="meeting_prep",
        firm_id=uuid.uuid4(),
        trigger="scheduled",
        event_data={},
        config=config or {},
        is_dry_run=False,
        requested_at=_dt.datetime.now(_dt.UTC),
    )


def test_plugin_metadata_validates() -> None:
    """The base class's validate_metadata catches missing fields."""
    MeetingPrepPlugin.validate_metadata()


def test_plugin_triggers_include_scheduled_and_manual() -> None:
    assert "scheduled" in MeetingPrepPlugin.triggers
    assert "manual" in MeetingPrepPlugin.triggers


def test_plugin_has_schedule_cron() -> None:
    """Scheduled trigger requires schedule_cron set (validate_metadata
    enforces this; this test pins the actual cadence)."""
    assert MeetingPrepPlugin.schedule_cron == "0 7 * * *"


def test_plugin_enables_calendar_memory_kg_reasoning() -> None:
    cats = MeetingPrepPlugin.enabled_tool_categories
    assert "calendar" in cats
    assert "memory" in cats
    assert "kg" in cats
    assert "reasoning" in cats
    # Email tools intentionally not included.
    assert "email" not in cats


def test_plugin_allow_side_effects_true() -> None:
    """meeting_prep produces approval items (side-effect tools)."""
    assert MeetingPrepPlugin.allow_side_effects is True


def test_goal_includes_tools_and_window() -> None:
    goal = MeetingPrepPlugin.goal(_run())
    assert "calendar_list_events" in goal
    assert "meeting_brief_propose" in goal
    assert "kg_entity_lookup" in goal
    assert "memory_query" in goal
    # Default look-ahead window is mentioned.
    assert "36" in goal


def test_goal_respects_look_ahead_config() -> None:
    goal = MeetingPrepPlugin.goal(_run(config={"look_ahead_hours": 48}))
    assert "48" in goal


def test_system_prompt_includes_threshold() -> None:
    prompt = MeetingPrepPlugin.system_prompt(_run()) or ""
    assert "0.85" in prompt


def test_register_in_builtin_plugins() -> None:
    """The plugin shows up in the registry assembled at startup."""
    from coworker.plugins.base import PluginRegistry
    from coworker.plugins.builtin import register_builtin_plugins

    reg = PluginRegistry()
    register_builtin_plugins(reg)
    assert reg.get("meeting_prep") is MeetingPrepPlugin
