"""Reasoning-category builtin: ``get_firm_info``.

Trivial tool — returns the firm's identity fields. Useful for
plugins that need to surface firm-aware language ("good morning,
[firm name] team") without the model fabricating details.
"""
from typing import Any

from pydantic import BaseModel

from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolRegistry,
)
from coworker.security.sanitise import sanitise_and_wrap


class GetFirmInfoInput(BaseModel):
    """No parameters — the firm is bound by ``firm_context``."""


async def _get_firm_info_handler(
    inp: GetFirmInfoInput, ctx: AgentContext
) -> dict[str, Any]:
    """Return firm identity; wrap the human-typed ``name``.

    Sanitised: ``name`` — typed by the principal at bootstrap;
    in principle attacker-controllable if the bootstrap CLI is
    ever automated from form input. Cheap defence in depth.

    Untouched: ``slug`` (URL-safe by construction), ``abn``
    (validated 11-digit string), ``timezone`` (IANA name),
    ``shadow_mode`` (boolean).
    """
    firm = ctx.firm
    wrapped_name, _ = sanitise_and_wrap(firm.name, max_length=200)
    return {
        "name": wrapped_name,
        "slug": firm.slug,
        "abn": firm.abn,
        "timezone": firm.timezone,
        "shadow_mode": firm.shadow_mode,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="get_firm_info",
            description=(
                "Return the firm's identity (name, slug, ABN, "
                "timezone, shadow_mode flag). Use when drafting "
                "communications that reference the firm or when "
                "the model needs to know whether actions will "
                "actually go out (shadow_mode=True means writes "
                "are blocked at the connector layer)."
            ),
            category="reasoning",
            input_model=GetFirmInfoInput,
            handler=_get_firm_info_handler,
            cost_estimate_cents=0,
        )
    )
