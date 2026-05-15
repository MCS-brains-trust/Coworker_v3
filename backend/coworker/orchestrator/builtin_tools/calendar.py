"""Calendar-category builtin tools.

One read tool today; write tools (create_event, update_event,
respond_to_event) land later when a plugin actually needs them.
``calendar_list_events`` is the bedrock for the Phase 12 +
meeting_prep plugin: it returns expanded recurrences in a tz-
bounded window so the agent can ask "what's on the principal's
calendar tomorrow?" without paginating raw Graph responses.

Requires ``ctx.graph_ctx`` (a per-user GraphContext from the
Phase 3C-4 wiring). When absent, the handler raises ToolError so
the agent loop continues with a Claude-visible error.
"""
import datetime as _dt
from typing import Any

from pydantic import BaseModel, Field

from coworker.graph.calendar import list_calendar_events
from coworker.graph.context import GraphContext
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolError,
    ToolRegistry,
)

_DEFAULT_TOP = 50
_MAX_TOP = 1000


def _require_graph_ctx(ctx: AgentContext, tool_name: str) -> GraphContext:
    if ctx.graph_ctx is None:
        raise ToolError(
            f"{tool_name} requires a Microsoft Graph context, which "
            "isn't available in this run (the orchestrator wasn't "
            "given a mailbox owner). Continue without it or escalate "
            "to a human."
        )
    return ctx.graph_ctx


class CalendarListEventsInput(BaseModel):
    start: _dt.datetime = Field(
        description=(
            "Inclusive lower bound for the event window. ISO-8601 "
            "with timezone offset; naive datetimes are rejected. "
            "For 'tomorrow', use 00:00 in the firm's timezone."
        ),
    )
    end: _dt.datetime = Field(
        description=(
            "Exclusive upper bound. Must be strictly after start."
        ),
    )
    top: int = Field(
        default=_DEFAULT_TOP, ge=1, le=_MAX_TOP,
        description=(
            "Page size, 1..1000. Graph caps calendarView at 1000; "
            "deeper pagination lands when a plugin needs it."
        ),
    )


async def _calendar_list_events_handler(
    inp: CalendarListEventsInput, ctx: AgentContext
) -> dict[str, Any]:
    graph_ctx = _require_graph_ctx(ctx, "calendar_list_events")
    events = await list_calendar_events(
        graph_ctx, start=inp.start, end=inp.end, top=inp.top,
    )
    return {
        "count": len(events),
        "events": [
            {
                "id": e.id,
                "subject": e.subject,
                "preview": e.preview,
                "start": e.start.isoformat(),
                "end": e.end.isoformat(),
                "location": e.location,
                "is_all_day": e.is_all_day,
                "is_cancelled": e.is_cancelled,
                "show_as": e.show_as,
                "organizer": (
                    {"email": e.organizer.email, "name": e.organizer.name}
                    if e.organizer
                    else None
                ),
                "attendees": [
                    {
                        "email": a.email,
                        "name": a.name,
                        "attendee_type": a.attendee_type,
                        "response_status": a.response_status,
                    }
                    for a in e.attendees
                ],
                "online_meeting_url": e.online_meeting_url,
                "web_link": e.web_link,
            }
            for e in events
        ],
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="calendar_list_events",
            description=(
                "Return calendar events for the signed-in user "
                "between start and end (recurring instances expanded). "
                "Use to answer 'what's on the calendar', to find "
                "open slots, or to inform a meeting brief. Read-only."
            ),
            category="calendar",
            input_model=CalendarListEventsInput,
            handler=_calendar_list_events_handler,
            cost_estimate_cents=0,
        )
    )
