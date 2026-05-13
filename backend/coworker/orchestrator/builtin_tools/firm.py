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


class GetFirmInfoInput(BaseModel):
    """No parameters — the firm is bound by ``firm_context``."""


async def _get_firm_info_handler(
    inp: GetFirmInfoInput, ctx: AgentContext
) -> dict[str, Any]:
    firm = ctx.firm
    return {
        "name": firm.name,
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
