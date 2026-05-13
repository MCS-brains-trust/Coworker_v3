"""Reasoning-category builtin: ``get_today_date``.

Trivial tool — but Claude lacks reliable date awareness across
sessions, so a date lookup is a frequent must-call. Returns
date components in the firm's configured timezone so deadline
calculations ("when is the next quarterly BAS?") use the right
local-day boundary.
"""
import datetime as _dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolRegistry,
)


class GetTodayDateInput(BaseModel):
    timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone name (e.g. 'Australia/Sydney'). Omit to "
            "use the firm's configured timezone."
        ),
    )


async def _get_today_date_handler(
    inp: GetTodayDateInput, ctx: AgentContext
) -> dict[str, Any]:
    tz_name = inp.timezone or ctx.firm.timezone or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = _dt.datetime.now(tz)
    return {
        "timezone": tz_name,
        "date": now.date().isoformat(),
        "datetime": now.isoformat(timespec="seconds"),
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "weekday": now.strftime("%A"),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="get_today_date",
            description=(
                "Return today's date and weekday in the firm's "
                "timezone. Use whenever the answer depends on the "
                "current date (deadline calculations, 'this "
                "quarter', 'next Monday')."
            ),
            category="reasoning",
            input_model=GetTodayDateInput,
            handler=_get_today_date_handler,
            cost_estimate_cents=0,
        )
    )
