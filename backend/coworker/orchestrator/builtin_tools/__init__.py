"""Builtin tool catalogue.

``register_builtin_tools(registry)`` populates a registry with the
read-only / safe tools that every plugin can use without further
gating. Write-side tools (create_draft, send_reminder,
create_envelope, …) live behind their connector modules' shadow-
mode guards and are registered by the Phase 6 plugin loader
based on each plugin's declared categories.

What's here vs deferred:

- memory (memory_query) — wraps Phase 4C HybridRetriever
- kg (kg_entity_lookup, kg_get_relationships) — pure DB
- reasoning (get_today_date, get_firm_info) — trivial
- email (email_get_message, email_create_draft,
  email_propose_draft, email_mark_as_read)
- calendar (calendar_list_events) — Phase 12-1

Not yet wired (will arrive when Phase 6 needs them):

- xpm_* (XPM client / job / invoice / note)
- fusesign_* (envelope ops)
- teams_* (webhook posts)
- vision_* (Phase 7)
- approval_* (Phase 9)
"""
from coworker.orchestrator.builtin_tools import (
    calendar,
    clock,
    email,
    firm,
    kg,
    memory,
)
from coworker.orchestrator.tools import ToolRegistry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Populate ``registry`` with every builtin tool.

    Read-only tools (memory, kg, firm info, clock, calendar) and
    the email tools (one read + two shadow-guarded writes + one
    approval-queue proposer) all register here. PluginExecutor
    handles dry-run + per-plugin category filtering before passing
    the slice to the engine.
    """
    memory.register(registry)
    kg.register(registry)
    firm.register(registry)
    clock.register(registry)
    email.register(registry)
    calendar.register(registry)


__all__ = ["register_builtin_tools"]
