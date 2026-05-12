"""Integration tests for ``coworker.graph.calendar.list_calendar_events``.

Pattern matches ``test_graph_mail.py``: direct call into the helper
under firm_context, Microsoft Graph mocked via respx, real DB.
"""
import asyncio
import datetime as _dt
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.calendar import (
    CalendarEvent,
    list_calendar_events,
)
from coworker.graph.context import GraphContext
from coworker.graph.mail import InboxAddress
from coworker.security.encryption import encrypt_str

_GRAPH_CALENDAR_VIEW_URL = "https://graph.microsoft.com/v1.0/me/calendarView"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def graph_calendar_environment(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sessionmaker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    created_firm_ids: list[uuid.UUID] = []
    try:
        yield {"sessionmaker": sessionmaker, "created_firm_ids": created_firm_ids}
    finally:
        for firm_id in created_firm_ids:
            asyncio.run(_delete_test_firm(sessionmaker, firm_id))
        asyncio.run(engine.dispose())


async def _delete_test_firm(sessionmaker, firm_id: uuid.UUID) -> None:
    tables = ("firms", "users", "audit_log")
    async with sessionmaker() as session:
        for t in tables:
            await session.execute(text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
        try:
            await session.execute(
                text("DELETE FROM audit_log WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM users WHERE firm_id = :id"), {"id": str(firm_id)}
            )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"), {"id": str(firm_id)}
            )
            await session.commit()
        finally:
            for t in tables:
                await session.execute(
                    text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
                )
            await session.commit()


def _seed(sessionmaker, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Calendar Test Firm",
                    slug=slug,
                    azure_tenant_id=str(uuid.uuid4()),
                    azure_client_id=str(uuid.uuid4()),
                    azure_client_secret_ciphertext=encrypt_str(
                        "secret", firm_id=firm_id_str
                    ),
                )
            )
            await session.flush()
            user = User(
                firm_id=firm_id,
                azure_object_id=uuid.uuid4().hex,
                upn=f"cal-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Calendar Test User",
                ms_access_token_ciphertext=encrypt_str(
                    "test-access", firm_id=firm_id_str
                ),
                ms_refresh_token_ciphertext=encrypt_str(
                    "test-refresh", firm_id=firm_id_str
                ),
                ms_token_expires_at=_dt.datetime.now(_dt.UTC)
                + _dt.timedelta(hours=1),
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            await session.commit()
            return firm_id, user_id

    return asyncio.run(_run())


def _audit_entries(sessionmaker, firm_id: uuid.UUID) -> list[AuditLogEntry]:
    async def _run() -> list[AuditLogEntry]:
        async with sessionmaker() as session, firm_context(firm_id):
            result = await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.firm_id == firm_id)
                .order_by(AuditLogEntry.id.asc())
            )
            return list(result.scalars().all())

    return asyncio.run(_run())


def _run_with_ctx(sessionmaker, firm_id, user_id, body):
    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="bearer-xyz", session=session
            )
            return await body(ctx)

    return asyncio.run(_run())


def _sample_event(
    *,
    event_id: str = "ev-1",
    subject: str = "Client meeting",
    start: str = "2026-05-13T14:00:00.0000000",
    end: str = "2026-05-13T15:00:00.0000000",
    is_all_day: bool = False,
    is_cancelled: bool = False,
    show_as: str = "busy",
    organizer_email: str | None = "alice@example.com",
    location_name: str | None = "Office",
    attendees: list[dict] | None = None,
    online_meeting_join_url: str | None = None,
    legacy_online_meeting_url: str | None = None,
    web_link: str = "https://outlook.office365.com/owa/?itemid=...",
) -> dict:
    """Construct a Graph-shaped event dict for /me/calendarView."""
    event: dict = {
        "id": event_id,
        "subject": subject,
        "bodyPreview": "Quarterly review",
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "isAllDay": is_all_day,
        "isCancelled": is_cancelled,
        "showAs": show_as,
        "webLink": web_link,
    }
    if organizer_email is not None:
        event["organizer"] = {
            "emailAddress": {"address": organizer_email, "name": "Alice"},
        }
    if location_name is not None:
        event["location"] = {"displayName": location_name}
    if attendees is not None:
        event["attendees"] = attendees
    if online_meeting_join_url is not None:
        event["onlineMeeting"] = {"joinUrl": online_meeting_join_url}
    if legacy_online_meeting_url is not None:
        event["onlineMeetingUrl"] = legacy_online_meeting_url
    return event


# --------------------------- happy paths ------------------------------------


def test_list_calendar_events_returns_parsed_events_and_audits(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "value": [
            _sample_event(
                event_id="ev-1",
                subject="Quarterly review",
                attendees=[
                    {
                        "type": "required",
                        "status": {"response": "accepted"},
                        "emailAddress": {
                            "address": "bob@example.com",
                            "name": "Bob",
                        },
                    },
                    {
                        "type": "optional",
                        "status": {"response": "tentativelyAccepted"},
                        "emailAddress": {"address": "carol@example.com"},
                    },
                ],
            ),
            _sample_event(
                event_id="ev-2",
                subject="All hands",
                start="2026-05-14T09:00:00.0000000",
                end="2026-05-14T10:00:00.0000000",
                show_as="tentative",
                attendees=[],
            ),
        ]
    }

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 15, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> list[CalendarEvent]:
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(200, json=payload)
            )
            events = await list_calendar_events(ctx, start=start, end=end, top=10)
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer bearer-xyz"
        assert sent.url.params["startDateTime"] == "2026-05-13T00:00:00Z"
        assert sent.url.params["endDateTime"] == "2026-05-15T00:00:00Z"
        assert sent.url.params["$top"] == "10"
        assert sent.url.params["$orderby"] == "start/dateTime asc"
        return events

    events = _run_with_ctx(sm, firm_id, user_id, body)

    assert len(events) == 2
    first = events[0]
    assert isinstance(first, CalendarEvent)
    assert first.id == "ev-1"
    assert first.subject == "Quarterly review"
    assert first.show_as == "busy"
    assert first.is_cancelled is False
    assert first.organizer == InboxAddress(email="alice@example.com", name="Alice")
    assert first.location == "Office"
    assert first.start == _dt.datetime(2026, 5, 13, 14, tzinfo=_dt.UTC)
    assert first.end == _dt.datetime(2026, 5, 13, 15, tzinfo=_dt.UTC)
    assert len(first.attendees) == 2
    assert first.attendees[0].email == "bob@example.com"
    assert first.attendees[0].attendee_type == "required"
    assert first.attendees[0].response_status == "accepted"
    assert first.attendees[1].name is None
    assert first.attendees[1].response_status == "tentativelyAccepted"

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.calendar.list_events"]
    assert len(success) == 1
    assert success[0].payload["count"] == 2
    assert success[0].payload["top"] == 10
    assert success[0].payload["start"] == "2026-05-13T00:00:00Z"
    assert success[0].payload["end"] == "2026-05-15T00:00:00Z"


def test_list_calendar_events_empty_returns_empty_list(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-empty-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> list[CalendarEvent]:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(200, json={"value": []})
            )
            return await list_calendar_events(ctx, start=start, end=end)

    events = _run_with_ctx(sm, firm_id, user_id, body)
    assert events == []

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.calendar.list_events"]
    assert len(success) == 1
    assert success[0].payload["count"] == 0


def test_list_calendar_events_prefers_modern_online_meeting_join_url(
    graph_calendar_environment,
) -> None:
    """onlineMeeting.joinUrl takes precedence over legacy onlineMeetingUrl."""
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-online-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "value": [
            _sample_event(
                event_id="ev-online-both",
                online_meeting_join_url="https://teams.microsoft.com/modern",
                legacy_online_meeting_url="https://teams.microsoft.com/legacy",
            ),
            _sample_event(
                event_id="ev-online-legacy-only",
                start="2026-05-13T16:00:00.0000000",
                end="2026-05-13T17:00:00.0000000",
                legacy_online_meeting_url="https://teams.microsoft.com/legacy-only",
            ),
            _sample_event(
                event_id="ev-no-online",
                start="2026-05-13T18:00:00.0000000",
                end="2026-05-13T19:00:00.0000000",
            ),
        ]
    }

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> list[CalendarEvent]:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await list_calendar_events(ctx, start=start, end=end)

    events = _run_with_ctx(sm, firm_id, user_id, body)
    assert events[0].online_meeting_url == "https://teams.microsoft.com/modern"
    assert events[1].online_meeting_url == "https://teams.microsoft.com/legacy-only"
    assert events[2].online_meeting_url is None


def test_list_calendar_events_drops_attendees_without_email(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-noaddr-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "value": [
            _sample_event(
                attendees=[
                    {"type": "required", "status": {"response": "accepted"}},  # no emailAddress
                    {
                        "type": "required",
                        "status": {"response": "accepted"},
                        "emailAddress": {"address": "real@example.com"},
                    },
                ]
            )
        ]
    }

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> list[CalendarEvent]:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await list_calendar_events(ctx, start=start, end=end)

    events = _run_with_ctx(sm, firm_id, user_id, body)
    assert len(events[0].attendees) == 1
    assert events[0].attendees[0].email == "real@example.com"


def test_list_calendar_events_unknown_show_as_falls_back(
    graph_calendar_environment,
) -> None:
    """Microsoft sometimes ships new ShowAs values — surface as 'unknown'."""
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-showas-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {"value": [_sample_event(show_as="thinking")]}

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> list[CalendarEvent]:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await list_calendar_events(ctx, start=start, end=end)

    events = _run_with_ctx(sm, firm_id, user_id, body)
    assert events[0].show_as == "unknown"


def test_list_calendar_events_converts_input_to_utc(
    graph_calendar_environment,
) -> None:
    """Non-UTC tz-aware input should still result in UTC Z-suffixed wire values."""
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-tz-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    aest = _dt.timezone(_dt.timedelta(hours=10))
    start = _dt.datetime(2026, 5, 13, 10, 0, 0, tzinfo=aest)  # 00:00 UTC
    end = _dt.datetime(2026, 5, 14, 10, 0, 0, tzinfo=aest)  # next day 00:00 UTC

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(200, json={"value": []})
            )
            await list_calendar_events(ctx, start=start, end=end)
        sent = route.calls.last.request
        assert sent.url.params["startDateTime"] == "2026-05-13T00:00:00Z"
        assert sent.url.params["endDateTime"] == "2026-05-14T00:00:00Z"

    _run_with_ctx(sm, firm_id, user_id, body)


# --------------------------- failure paths ----------------------------------


def test_list_calendar_events_401_raises_auth_error_and_audits(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(401, json={"error": "unauthorized"})
            )
            with pytest.raises(ConnectorAuthError):
                await list_calendar_events(ctx, start=start, end=end)

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.calendar.list_events_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_401"
    assert failed[0].payload["start"] == "2026-05-13T00:00:00Z"
    assert failed[0].payload["end"] == "2026-05-14T00:00:00Z"


def test_list_calendar_events_429_with_retry_after(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-429-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(
                    429,
                    headers={"Retry-After": "20"},
                    json={"error": "throttled"},
                )
            )
            with pytest.raises(ConnectorRateLimited) as excinfo:
                await list_calendar_events(ctx, start=start, end=end)
            assert excinfo.value.retry_after == 20.0

    _run_with_ctx(sm, firm_id, user_id, body)


def test_list_calendar_events_5xx_raises_transient(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                return_value=httpx.Response(503)
            )
            with pytest.raises(ConnectorTransient):
                await list_calendar_events(ctx, start=start, end=end)

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.calendar.list_events_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_5xx"


def test_list_calendar_events_network_error_raises_transient_and_audits(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_GRAPH_CALENDAR_VIEW_URL).mock(
                side_effect=httpx.ConnectError("no network")
            )
            with pytest.raises(ConnectorTransient):
                await list_calendar_events(ctx, start=start, end=end)

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.calendar.list_events_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


# --------------------------- input validation -------------------------------


def test_list_calendar_events_rejects_naive_datetimes(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-naive-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    naive_start = _dt.datetime(2026, 5, 13)
    naive_end = _dt.datetime(2026, 5, 14)
    aware = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await list_calendar_events(ctx, start=naive_start, end=aware)
        with pytest.raises(ValueError):
            await list_calendar_events(ctx, start=aware, end=naive_end)

    _run_with_ctx(sm, firm_id, user_id, body)


def test_list_calendar_events_rejects_end_le_start(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-order-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    same = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    before = _dt.datetime(2026, 5, 12, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await list_calendar_events(ctx, start=start, end=same)
        with pytest.raises(ValueError):
            await list_calendar_events(ctx, start=start, end=before)

    _run_with_ctx(sm, firm_id, user_id, body)


def test_list_calendar_events_rejects_invalid_top(
    graph_calendar_environment,
) -> None:
    sm = graph_calendar_environment["sessionmaker"]
    created = graph_calendar_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"cal-top-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    start = _dt.datetime(2026, 5, 13, tzinfo=_dt.UTC)
    end = _dt.datetime(2026, 5, 14, tzinfo=_dt.UTC)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await list_calendar_events(ctx, start=start, end=end, top=0)
        with pytest.raises(ValueError):
            await list_calendar_events(ctx, start=start, end=end, top=1001)

    _run_with_ctx(sm, firm_id, user_id, body)
