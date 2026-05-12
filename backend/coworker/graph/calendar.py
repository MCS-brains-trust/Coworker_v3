"""Microsoft Graph calendar operations.

``list_calendar_events`` returns the signed-in user's events between
two timestamps, with recurring series already expanded into their
individual instances (Graph's ``/me/calendarView`` does that for us
— the alternative endpoint ``/me/events`` returns the recurrence
master and is hard to consume from Phase 12's calendar-awareness
tools).

Caller invariant: ``ctx.session`` has ``firm_context(ctx.firm.id)``
already entered. ``graph_context`` enters it on the request scope.

Times on the wire:

- The request always sends UTC ISO-8601 (``...Z``). Callers can pass
  any tz-aware datetime; we convert to UTC before formatting. Naive
  datetimes raise ``ValueError`` — Graph would accept them and assume
  the user's mailbox timezone, which silently produces wrong results
  when the user's timezone differs from where the code is running.
- The response is requested in UTC (no ``Prefer: outlook.timezone``
  header, which is Graph's default). We surface ``start`` and ``end``
  as tz-aware UTC datetimes.
"""
import datetime as _dt
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from coworker.connectors.exceptions import ConnectorTransient
from coworker.graph.context import GraphContext
from coworker.graph.errors import audit_failure, raise_for_graph_status
from coworker.graph.mail import InboxAddress
from coworker.graph.rate_limit import get_rate_limiter
from coworker.security.audit import append_audit

_CALENDAR_VIEW_ENDPOINT = "https://graph.microsoft.com/v1.0/me/calendarView"
_DEFAULT_TOP = 50
_MAX_TOP = 1000

_SELECT_FIELDS = ",".join([
    "id",
    "subject",
    "bodyPreview",
    "start",
    "end",
    "location",
    "isAllDay",
    "isCancelled",
    "showAs",
    "organizer",
    "attendees",
    "onlineMeeting",
    "onlineMeetingUrl",
    "webLink",
])

AttendeeType = Literal["required", "optional", "resource"]
AttendeeResponseStatus = Literal[
    "none",
    "organizer",
    "tentativelyAccepted",
    "accepted",
    "declined",
    "notResponded",
]
ShowAs = Literal[
    "free",
    "tentative",
    "busy",
    "oof",
    "workingElsewhere",
    "unknown",
]


class CalendarAttendee(BaseModel):
    """One person on an event's attendee list.

    Distinct shape from ``InboxAddress`` because attendees carry the
    invitation type (required / optional / resource) and the response
    status — both useful for "who's actually coming" and "is the
    organiser free if I move this".
    """

    model_config = ConfigDict(frozen=True)

    email: str
    name: str | None = None
    attendee_type: AttendeeType
    response_status: AttendeeResponseStatus


class CalendarEvent(BaseModel):
    """One calendar event, narrowed from Graph's wide schema.

    Stable contract for plugins. Phase 12's calendar-awareness tools
    (``calendar_get_user_events``, ``calendar_get_firm_availability``)
    consume this directly; the Smart Responder uses ``show_as`` to
    decide whether a slot is offerable.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    subject: str
    preview: str
    start: _dt.datetime
    end: _dt.datetime
    location: str | None = None
    is_all_day: bool
    is_cancelled: bool
    show_as: ShowAs
    organizer: InboxAddress | None = None
    attendees: list[CalendarAttendee] = Field(default_factory=list)
    online_meeting_url: str | None = Field(
        default=None,
        description=(
            "Join URL for a Teams / Skype online meeting. Pulled from "
            "``onlineMeeting.joinUrl`` when present, falling back to "
            "the legacy ``onlineMeetingUrl`` field; None when the "
            "event has no online component."
        ),
    )
    web_link: str | None = None


async def list_calendar_events(
    ctx: GraphContext,
    *,
    start: _dt.datetime,
    end: _dt.datetime,
    top: int = _DEFAULT_TOP,
) -> list[CalendarEvent]:
    """Return events in ``[start, end)`` with recurrences expanded.

    Args:
        ctx: per-request Graph bundle. ``ctx.session`` must already
            be inside ``firm_context(ctx.firm.id)``.
        start, end: tz-aware datetimes bounding the range. Naive
            datetimes are rejected (Graph would silently assume the
            mailbox's timezone and produce wrong results).
        top: page size, 1 ≤ top ≤ 1000. Graph caps ``$top`` at 1000
            for calendarView; deeper pagination (delta or ``@odata.nextLink``)
            lands in a later sub-phase alongside the Phase 4 indexer.

    Raises:
        ConnectorAuthError: 401 / 403 / other unhandled 4xx.
        ConnectorRateLimited: 429.
        ConnectorTransient: 5xx, timeout, or network error.
        ValueError: ``start`` or ``end`` is tz-naive, ``end <= start``,
            or ``top`` is outside [1, 1000].
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError(
            "start and end must be tz-aware datetimes; got "
            f"start.tzinfo={start.tzinfo!r}, end.tzinfo={end.tzinfo!r}"
        )
    if end <= start:
        raise ValueError(
            f"end must be strictly after start (got start={start.isoformat()}, "
            f"end={end.isoformat()})"
        )
    if top < 1 or top > _MAX_TOP:
        raise ValueError(f"top must be between 1 and {_MAX_TOP}, got {top}")

    mailbox_id = str(ctx.user.id)
    firm_id_str = str(ctx.firm.id)
    user_id_str = str(ctx.user.id)
    action = "graph.calendar.list_events"

    start_utc = _to_utc_z(start)
    end_utc = _to_utc_z(end)

    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(mailbox_id):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    _CALENDAR_VIEW_ENDPOINT,
                    params={
                        "startDateTime": start_utc,
                        "endDateTime": end_utc,
                        "$top": top,
                        "$orderby": "start/dateTime asc",
                        "$select": _SELECT_FIELDS,
                    },
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                )
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=user_id_str,
                action=action,
                reason="network_error",
                extra={"start": start_utc, "end": end_utc},
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    await raise_for_graph_status(
        response,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=user_id_str,
        action=action,
        allow_not_found=False,
        extra={"start": start_utc, "end": end_utc},
    )

    body = response.json()
    raw_events = body.get("value", [])
    events = [_parse_event(e) for e in raw_events]

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action=action,
        payload={
            "user_id": user_id_str,
            "start": start_utc,
            "end": end_utc,
            "count": len(events),
            "top": top,
        },
    )
    await ctx.session.commit()

    return events


def _parse_event(raw: dict[str, Any]) -> CalendarEvent:
    """Map one Graph event dict into a ``CalendarEvent``."""
    location_block = raw.get("location") or {}
    location = location_block.get("displayName") or None

    organizer_block = raw.get("organizer")
    organizer: InboxAddress | None = None
    if organizer_block:
        addr = organizer_block.get("emailAddress") or {}
        email = addr.get("address")
        if email:
            organizer = InboxAddress(email=email, name=addr.get("name"))

    raw_attendees = raw.get("attendees") or []
    attendees = [
        _parse_attendee(a)
        for a in raw_attendees
        if _attendee_has_email(a)
    ]

    online_meeting_url = _resolve_online_meeting_url(raw)

    show_as_raw = raw.get("showAs") or "unknown"
    show_as: ShowAs
    if show_as_raw in (
        "free",
        "tentative",
        "busy",
        "oof",
        "workingElsewhere",
        "unknown",
    ):
        show_as = show_as_raw  # type: ignore[assignment]
    else:
        # Graph occasionally returns new strings (e.g. when Microsoft
        # adds a status). Fall back to ``unknown`` rather than crash
        # on an otherwise readable event.
        show_as = "unknown"

    return CalendarEvent(
        id=raw["id"],
        subject=raw.get("subject") or "",
        preview=raw.get("bodyPreview") or "",
        start=_parse_event_datetime(raw["start"]),
        end=_parse_event_datetime(raw["end"]),
        location=location,
        is_all_day=bool(raw.get("isAllDay", False)),
        is_cancelled=bool(raw.get("isCancelled", False)),
        show_as=show_as,
        organizer=organizer,
        attendees=attendees,
        online_meeting_url=online_meeting_url,
        web_link=raw.get("webLink"),
    )


def _attendee_has_email(raw: dict[str, Any]) -> bool:
    addr = (raw.get("emailAddress") or {}).get("address")
    return bool(addr)


def _parse_attendee(raw: dict[str, Any]) -> CalendarAttendee:
    addr = raw.get("emailAddress") or {}
    status_block = raw.get("status") or {}

    attendee_type_raw = raw.get("type") or "required"
    if attendee_type_raw not in ("required", "optional", "resource"):
        attendee_type_raw = "required"

    response_raw = status_block.get("response") or "none"
    if response_raw not in (
        "none",
        "organizer",
        "tentativelyAccepted",
        "accepted",
        "declined",
        "notResponded",
    ):
        response_raw = "none"

    return CalendarAttendee(
        email=addr["address"],
        name=addr.get("name"),
        attendee_type=attendee_type_raw,  # type: ignore[arg-type]
        response_status=response_raw,  # type: ignore[arg-type]
    )


def _resolve_online_meeting_url(raw: dict[str, Any]) -> str | None:
    """Prefer ``onlineMeeting.joinUrl``; fall back to legacy ``onlineMeetingUrl``."""
    online_meeting = raw.get("onlineMeeting")
    if isinstance(online_meeting, dict):
        join_url = online_meeting.get("joinUrl")
        if join_url:
            return str(join_url)
    legacy = raw.get("onlineMeetingUrl")
    if legacy:
        return str(legacy)
    return None


def _parse_event_datetime(block: dict[str, Any]) -> _dt.datetime:
    """Parse Graph's ``{dateTime, timeZone}`` event-time block.

    Graph serialises ``dateTime`` with seven fractional digits; Python
    only parses up to six. We trim before handing off to
    ``fromisoformat``. ``timeZone`` is always ``UTC`` because we do
    not set the ``Prefer: outlook.timezone`` header on the request;
    anything else indicates a misconfigured caller and we raise so
    the bug surfaces immediately rather than producing silently
    wrong times.
    """
    dt_str = block["dateTime"]
    tz_name = block.get("timeZone", "UTC")
    if tz_name != "UTC":
        raise ValueError(
            f"unexpected timeZone in Graph calendar response: {tz_name!r}"
        )
    if "." in dt_str:
        base, frac = dt_str.split(".", 1)
        dt_str = f"{base}.{frac[:6]}"
    parsed = _dt.datetime.fromisoformat(dt_str)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _to_utc_z(dt: _dt.datetime) -> str:
    """Convert a tz-aware datetime to UTC ISO-8601 with trailing Z."""
    utc = dt.astimezone(_dt.UTC).replace(tzinfo=None)
    return utc.isoformat(timespec="seconds") + "Z"
