"""Unit tests for ``SmartResponderPlugin``.

Pure-Python; no DB / HTTP. End-to-end execution coverage comes
later when email tools are wired in; here we exercise the
plugin's metadata + goal/system_prompt construction + the
registry helper.
"""
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coworker.plugins.base import PluginRegistry, PluginRun
from coworker.plugins.builtin import (
    SmartResponderPlugin,
    register_builtin_plugins,
)
from coworker.plugins.builtin.smart_responder import (
    SmartResponderConfig,
    _expected_event_keys,
)


def _email_event(
    *,
    message_id: str = "msg-1",
    from_addr: str = "alice@acme.example",
    subject: str = "BAS Q1 query",
    preview: str = "Just wondering when the next BAS is due.",
) -> dict:
    return {
        "message_id": message_id,
        "from": from_addr,
        "subject": subject,
        "body_preview": preview,
    }


def _run(event: dict, config: dict | None = None) -> PluginRun:
    return PluginRun(
        plugin_name=SmartResponderPlugin.name,
        firm_id=uuid.uuid4(),
        trigger="email_received",
        event_data=event,
        config=config or {},
        is_dry_run=False,
        requested_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_plugin_metadata_well_formed() -> None:
    SmartResponderPlugin.validate_metadata()  # raises if not


def test_plugin_declares_email_received_trigger() -> None:
    assert "email_received" in SmartResponderPlugin.triggers


def test_plugin_allows_side_effects() -> None:
    # The plugin creates email drafts; side-effect tools must be
    # available outside dry-run mode.
    assert SmartResponderPlugin.allow_side_effects is True


def test_plugin_tool_categories_cover_the_full_workflow() -> None:
    cats = SmartResponderPlugin.enabled_tool_categories
    # Memory + KG for context; email for read/draft; reasoning for
    # today's date + firm info.
    assert "memory" in cats
    assert "kg" in cats
    assert "email" in cats
    assert "reasoning" in cats


# ---------------------------------------------------------------------------
# Goal construction
# ---------------------------------------------------------------------------


def test_goal_includes_event_fields_verbatim() -> None:
    run = _run(_email_event(
        message_id="AAMk-1234",
        from_addr="bob@beta.example",
        subject="FBT calculation request",
        preview="Hi team, can you confirm the FBT for my company car?",
    ))
    goal = SmartResponderPlugin.goal(run)
    assert "AAMk-1234" in goal
    assert "bob@beta.example" in goal
    assert "FBT calculation request" in goal
    assert "FBT for my company car" in goal


def test_goal_handles_missing_event_fields_gracefully() -> None:
    """A malformed event shouldn't crash goal construction."""
    run = _run({})
    goal = SmartResponderPlugin.goal(run)
    # The goal renders unknown placeholders rather than raising.
    assert "<unknown>" in goal


def test_goal_omits_preview_block_when_preview_empty() -> None:
    run = _run(_email_event(preview=""))
    goal = SmartResponderPlugin.goal(run)
    assert "Preview:" not in goal


def test_goal_includes_workflow_steps() -> None:
    """The agent gets a starting sequence to follow."""
    run = _run(_email_event())
    goal = SmartResponderPlugin.goal(run)
    assert "email_get_message" in goal or "preview is enough" in goal
    assert "kg_entity_lookup" in goal
    assert "memory_query" in goal
    assert "email_create_draft" in goal


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_system_prompt_includes_default_threshold() -> None:
    run = _run(_email_event())
    prompt = SmartResponderPlugin.system_prompt(run)
    assert prompt is not None
    assert "0.85" in prompt


def test_system_prompt_reflects_config_threshold_override() -> None:
    run = _run(
        _email_event(),
        config={"confidence_threshold": 0.95},
    )
    prompt = SmartResponderPlugin.system_prompt(run)
    assert prompt is not None
    assert "0.95" in prompt


def test_system_prompt_includes_style_hint_when_set() -> None:
    run = _run(
        _email_event(),
        config={"style_hint": "very warm and friendly"},
    )
    prompt = SmartResponderPlugin.system_prompt(run)
    assert prompt is not None
    assert "very warm and friendly" in prompt


def test_system_prompt_omits_style_hint_when_not_set() -> None:
    run = _run(_email_event())
    prompt = SmartResponderPlugin.system_prompt(run)
    assert prompt is not None
    assert "Firm-specific voice override" not in prompt


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_config_default_threshold_is_in_unit_range() -> None:
    config = SmartResponderConfig()
    assert 0.0 <= config.confidence_threshold <= 1.0


def test_config_rejects_out_of_range_threshold() -> None:
    with pytest.raises(ValidationError):
        SmartResponderConfig(confidence_threshold=1.5)
    with pytest.raises(ValidationError):
        SmartResponderConfig(confidence_threshold=-0.1)


def test_expected_event_keys_documents_payload_shape() -> None:
    keys = _expected_event_keys()
    assert "message_id" in keys
    assert "from" in keys
    assert "subject" in keys


# ---------------------------------------------------------------------------
# Registry assembly
# ---------------------------------------------------------------------------


def test_register_builtin_plugins_adds_smart_responder() -> None:
    reg = PluginRegistry()
    register_builtin_plugins(reg)
    assert SmartResponderPlugin.name in reg
    assert reg.get(SmartResponderPlugin.name) is SmartResponderPlugin


def test_register_builtin_plugins_idempotent_via_fresh_registry() -> None:
    """A fresh registry can be populated independently from another."""
    reg1 = PluginRegistry()
    register_builtin_plugins(reg1)
    reg2 = PluginRegistry()
    register_builtin_plugins(reg2)
    assert SmartResponderPlugin.name in reg1
    assert SmartResponderPlugin.name in reg2
