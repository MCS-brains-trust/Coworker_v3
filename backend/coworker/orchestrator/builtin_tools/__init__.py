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

Not yet wired (will arrive when Phase 6 needs them):

- email_* (Graph mail read/draft/mark_read)
- calendar_* (Graph calendar)
- xpm_* (XPM client / job / invoice / note)
- fusesign_* (envelope ops)
- teams_* (webhook posts)
- vision_* (Phase 7)
- approval_* (Phase 9)
"""
from coworker.orchestrator.builtin_tools import clock, firm, kg, memory
from coworker.orchestrator.tools import ToolRegistry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Populate ``registry`` with the read-only / safe builtin tools."""
    memory.register(registry)
    kg.register(registry)
    firm.register(registry)
    clock.register(registry)


__all__ = ["register_builtin_tools"]
