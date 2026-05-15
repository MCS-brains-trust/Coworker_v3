"""MeetingPrepPlugin — Phase 12-2.

Once a day (cron ``0 7 * * *`` — 07:00 firm-time) the scheduler
fires this plugin. The agent loop:

1. Looks at the calendar for [now + 12h, now + 36h] — that
   captures "everything scheduled for tomorrow" regardless of
   when the cron fired.
2. For each event that's not all-day / cancelled / private,
   queries memory and the knowledge graph for context on the
   attendees and the subject.
3. Calls ``meeting_brief_propose`` once per event to write an
   approval item. The principal reads the brief from the queue
   ahead of the meeting.

The plugin also exposes the ``manual`` trigger so a principal
who wants a brief on demand can fire it via the Phase 13
manual-trigger API (when that lands).

Tool categories: calendar (list + propose), memory (past
interactions), kg (entity context), reasoning (today's date +
firm info). Email tools intentionally not included — meeting
prep doesn't draft replies.
"""
from pydantic import BaseModel, Field

from coworker.plugins.base import OrchestratorPlugin, PluginRun

_DEFAULT_BUDGET_CENTS = 30  # $0.30 / day per firm — multi-event runs


class MeetingPrepConfig(BaseModel):
    """Per-firm configuration."""

    look_ahead_hours: int = Field(
        default=36, ge=12, le=72,
        description=(
            "How far ahead to look. Default 36h catches everything "
            "scheduled for tomorrow regardless of when the 7am cron "
            "fired vs. the meeting time."
        ),
    )
    confidence_threshold: float = Field(
        default=0.85, ge=0.0, le=1.0,
        description=(
            "Above this confidence the brief is auto-approved on "
            "insert. Briefs are informational so a high default is "
            "safe — the principal can always re-read in the queue."
        ),
    )


class MeetingPrepPlugin(OrchestratorPlugin):
    """Daily pre-meeting brief generator."""

    name = "meeting_prep"
    display_name = "Meeting Prep"
    description = (
        "Once a day, surfaces a brief for every meeting in the next "
        "24 hours: who's attending, the client context, recent "
        "interactions, open action items. Lands in the approval "
        "queue ahead of the meeting."
    )
    version = "0.1.0"
    triggers = frozenset({"scheduled", "manual"})
    schedule_cron = "0 7 * * *"
    enabled_tool_categories = frozenset(
        {"calendar", "memory", "kg", "reasoning"}
    )
    config_schema = MeetingPrepConfig
    cost_budget_cents = _DEFAULT_BUDGET_CENTS
    allow_side_effects = True  # produces approval items

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        look_ahead = int(run.config.get("look_ahead_hours", 36))
        return (
            "Prepare briefs for upcoming meetings.\n\n"
            "Steps:\n\n"
            "1. Call get_today_date to ground yourself in the firm's "
            "current local date/time.\n"
            f"2. Call calendar_list_events for the window starting "
            f"12 hours from now and ending {look_ahead} hours from "
            f"now. Skip is_all_day events, is_cancelled events, "
            f"and events without external attendees (the principal "
            f"doesn't need a brief on internal 1:1s).\n"
            "3. For each remaining event, gather context:\n"
            "   - kg_entity_lookup on each external attendee's "
            "company / contact name.\n"
            "   - memory_query for two to three targeted queries "
            "(attendee names, meeting subject, distinctive phrases).\n"
            "4. Compose a short brief (3-5 paragraphs or a short "
            "bulleted list, HTML) covering: who's attending, the "
            "client context, recent interactions, open action items "
            "or asks the principal should prepare for.\n"
            "5. Call meeting_brief_propose once per meeting with the "
            "event_id, subject, start, end, attendees, brief_html, "
            "and a one-line summary for the inbox.\n\n"
            "End the run after every eligible meeting has a brief. "
            "If there are no upcoming meetings, end the run without "
            "proposing anything — an empty queue is the right "
            "outcome for a quiet day."
        )

    @classmethod
    def system_prompt(cls, run: PluginRun) -> str | None:
        threshold = run.config.get("confidence_threshold", 0.85)
        return (
            "You are an accounting practice assistant preparing "
            "the principal for tomorrow's meetings.\n\n"
            "Voice rules:\n"
            "- Be specific: name the client, reference the actual "
            "interactions you found in memory, surface the open "
            "ask if any.\n"
            "- Concise. The brief is read in 30 seconds before a "
            "meeting; long context dumps don't help.\n"
            "- Never invent. If memory has nothing on an attendee "
            "say so explicitly — \"first interaction\" is useful "
            "information.\n\n"
            f"Self-consistency target: {threshold:.2f}. Briefs are "
            "informational, so high-confidence briefs auto-approve "
            "and land in the queue as already-seen; below the "
            "threshold the principal sees a 'review' badge."
        )
