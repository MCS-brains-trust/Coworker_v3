"""Calendar-category builtin tools.

Two tools today:

- ``calendar_list_events`` (read): returns recurrence-expanded
  events in a tz-bounded window.
- ``meeting_brief_propose`` (write, approval-queue): writes a
  ``meeting_brief`` approval item summarising one upcoming
  meeting. Parallels ``email_propose_draft`` — no Outlook side
  effect; the principal reads the brief from the approval queue.

The list tool requires ``ctx.graph_ctx`` (a per-user GraphContext
from the Phase 3C-4 wiring). ``meeting_brief_propose`` also
needs it so the producing plugin can be tied to a specific
mailbox owner. When absent, both raise ToolError so the agent
loop continues with a Claude-visible error.
"""
import datetime as _dt
from typing import Any

from pydantic import BaseModel, Field

from coworker.approval.items import CreateApprovalInput, create_approval
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


class MeetingBriefProposeInput(BaseModel):
    """A meeting brief for principal review.

    Same approval-queue mechanism as ``email_propose_draft`` but
    with no dispatch step — meeting briefs live entirely in the
    approval inbox for the principal to read before the meeting.
    """

    event_id: str = Field(
        description=(
            "Graph event id the brief is for. Stored on the approval "
            "payload so the principal can click through to the "
            "calendar entry."
        ),
    )
    subject: str = Field(description="Meeting subject (for context).")
    start: _dt.datetime = Field(
        description="Meeting start time. Tz-aware ISO-8601.",
    )
    end: _dt.datetime = Field(
        description="Meeting end time. Tz-aware.",
    )
    brief_html: str = Field(
        description=(
            "The brief itself: HTML the principal will read in the "
            "approval queue. Should cover: who's attending, the "
            "client context from memory, recent interactions, any "
            "open action items. Keep it scannable — 3-5 short "
            "paragraphs or a short bulleted list."
        ),
    )
    summary: str = Field(
        max_length=500,
        description=(
            "One-line description for the inbox. Typically "
            "'Brief: <subject> with <client>' or similar."
        ),
    )
    attendees: list[str] = Field(
        default_factory=list,
        description=(
            "Attendee email addresses, for the principal's quick "
            "scan. Stored verbatim on the approval payload."
        ),
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Self-rated confidence in this brief, 0.0..1.0. Above "
            "the firm's auto-approve threshold the row is born "
            "approved (already-seen); the principal still finds it "
            "in the queue but doesn't need to click approve."
        ),
    )


async def _meeting_brief_propose_handler(
    inp: MeetingBriefProposeInput, ctx: AgentContext
) -> dict[str, Any]:
    graph_ctx = _require_graph_ctx(ctx, "meeting_brief_propose")
    payload: dict[str, Any] = {
        "event_id": inp.event_id,
        "subject": inp.subject,
        "start": inp.start.isoformat(),
        "end": inp.end.isoformat(),
        "brief_html": inp.brief_html,
        "attendees": list(inp.attendees),
        "owner_user_id": str(graph_ctx.user.id),
    }
    row = await create_approval(
        ctx.session,
        ctx.firm.id,
        input=CreateApprovalInput(
            plugin_name=str(ctx.metadata.get("plugin_name", "unknown")),
            category="meeting_brief",
            summary=inp.summary,
            payload=payload,
            trace_id=ctx.trace_id,
            confidence=inp.confidence,
        ),
    )
    return {
        "approval_item_id": str(row.id),
        "status": row.status,
        "summary": row.summary,
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
    registry.register(
        ToolDefinition(
            name="meeting_brief_propose",
            description=(
                "Propose a pre-meeting brief for principal review. "
                "Writes an approval_item with category=meeting_brief; "
                "no Outlook or Teams side effect. The principal reads "
                "the brief in the approval queue ahead of the meeting. "
                "Use this once per upcoming meeting that warrants "
                "context-gathering."
            ),
            category="calendar",
            input_model=MeetingBriefProposeInput,
            handler=_meeting_brief_propose_handler,
            cost_estimate_cents=0,
            side_effect=True,
        )
    )
