"""Unit tests for ``DeliveryStatusHandlerPlugin`` metadata + goal
construction (pre-pilot Task 3).

Plugin behaviour through the agent loop is covered separately in
the integration tests (``test_delivery_status_plugin_run.py``).
"""
import uuid
from datetime import UTC, datetime

from coworker.plugins.base import PluginRegistry, PluginRun
from coworker.plugins.builtin import (
    DeliveryStatusHandlerPlugin,
    register_builtin_plugins,
)


def _run(event_data: dict | None = None) -> PluginRun:
    return PluginRun(
        plugin_name=DeliveryStatusHandlerPlugin.name,
        firm_id=uuid.uuid4(),
        trigger="email_received",
        event_data=event_data or {"message_id": "msg-abc-123"},
        config={},
        is_dry_run=False,
        requested_at=datetime.now(UTC),
    )


def test_plugin_metadata_well_formed() -> None:
    DeliveryStatusHandlerPlugin.validate_metadata()  # raises if not


def test_plugin_listens_to_email_received() -> None:
    assert "email_received" in DeliveryStatusHandlerPlugin.triggers


def test_plugin_categories_include_email_and_approval() -> None:
    cats = DeliveryStatusHandlerPlugin.enabled_tool_categories
    assert "email" in cats
    assert "approval" in cats
    # No memory/kg/reasoning — the plugin is purely classify-and-mark.
    assert "memory" not in cats


def test_plugin_allows_side_effects() -> None:
    """approval_mark_delivery_failed is a side-effect tool;
    allow_side_effects must be True or the executor would filter
    it out."""
    assert DeliveryStatusHandlerPlugin.allow_side_effects is True


def test_goal_embeds_triggering_message_id() -> None:
    run = _run({"message_id": "AAMk-NDR-001"})
    goal = DeliveryStatusHandlerPlugin.goal(run)
    assert "AAMk-NDR-001" in goal


def test_goal_directs_get_message_then_classify() -> None:
    run = _run()
    goal = DeliveryStatusHandlerPlugin.goal(run)
    assert "email_get_message" in goal
    assert "multipart/report" in goal
    assert "report-type=delivery-status" in goal
    assert "approval_mark_delivery_failed" in goal


def test_goal_describes_in_reply_to_preference() -> None:
    """The goal teaches Claude which header to prefer for the
    original Message-ID; In-Reply-To wins over References."""
    goal = DeliveryStatusHandlerPlugin.goal(_run())
    assert "In-Reply-To" in goal
    assert "References" in goal


def test_goal_tells_claude_to_bail_fast_on_non_ndr() -> None:
    """For the common case (regular inbound email) Claude should
    end after the first tool call with a clear text response."""
    goal = DeliveryStatusHandlerPlugin.goal(_run())
    assert "not an NDR" in goal


def test_system_prompt_constrains_tool_use() -> None:
    prompt = DeliveryStatusHandlerPlugin.system_prompt(_run())
    assert prompt is not None
    assert "deterministic" in prompt.lower()
    assert "do not draft" in prompt.lower()


def test_register_builtin_plugins_includes_delivery_status_handler() -> None:
    reg = PluginRegistry()
    register_builtin_plugins(reg)
    assert DeliveryStatusHandlerPlugin.name in reg
    assert (
        reg.get(DeliveryStatusHandlerPlugin.name)
        is DeliveryStatusHandlerPlugin
    )
