"""Tests for calendar operations (sync and async)."""

import json

import httpx
import pytest
import respx

from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient
from ms_graph import calendar
from .conftest import (
    SAMPLE_CALENDAR_EVENT,
    SAMPLE_CALENDAR_EVENT_ALLDAY,
    SAMPLE_CALENDAR_EVENTS_RESPONSE,
    SAMPLE_CREATED_EVENT,
    SAMPLE_SCHEDULE_RESPONSE,
)


class TestCalendarSync:
    """Synchronous calendar operation tests."""

    @respx.mock
    def test_list_calendar_events(self):
        respx.get(f"{GRAPH_BASE_URL}/me/calendarView").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENTS_RESPONSE)
        )
        with GraphClient("tok") as client:
            events = calendar.list_calendar_events(
                client,
                start_datetime="2026-05-07T00:00:00Z",
                end_datetime="2026-05-14T00:00:00Z",
            )

        assert len(events) == 2
        assert events[0]["subject"] == "Sprint Planning"
        assert events[1]["isAllDay"] is True

    @respx.mock
    def test_list_calendar_events_empty(self):
        respx.get(f"{GRAPH_BASE_URL}/me/calendarView").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with GraphClient("tok") as client:
            events = calendar.list_calendar_events(
                client,
                start_datetime="2026-05-07T00:00:00Z",
                end_datetime="2026-05-14T00:00:00Z",
            )

        assert events == []

    @respx.mock
    def test_get_calendar_event(self):
        event_id = "AAMkAGI2-event-001"
        respx.get(f"{GRAPH_BASE_URL}/me/events/{event_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENT)
        )
        with GraphClient("tok") as client:
            event = calendar.get_calendar_event(client, event_id)

        assert event["subject"] == "Sprint Planning"
        assert event["attendees"][0]["emailAddress"]["address"] == "bob@example.com"

    @respx.mock
    def test_create_calendar_event_minimal(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/events").mock(
            return_value=httpx.Response(200, json=SAMPLE_CREATED_EVENT)
        )
        with GraphClient("tok") as client:
            result = calendar.create_calendar_event(
                client,
                subject="Design Review",
                start_datetime="2026-05-09T14:00:00",
                start_timezone="America/Los_Angeles",
                end_datetime="2026-05-09T15:00:00",
                end_timezone="America/Los_Angeles",
            )

        assert result["subject"] == "Design Review"
        payload = json.loads(route.calls[0].request.content)
        assert payload["subject"] == "Design Review"
        assert payload["start"]["timeZone"] == "America/Los_Angeles"
        assert "attendees" not in payload
        assert "body" not in payload
        assert "location" not in payload

    @respx.mock
    def test_create_calendar_event_full(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/events").mock(
            return_value=httpx.Response(200, json=SAMPLE_CREATED_EVENT)
        )
        with GraphClient("tok") as client:
            calendar.create_calendar_event(
                client,
                subject="Design Review",
                start_datetime="2026-05-09T14:00:00",
                start_timezone="UTC",
                end_datetime="2026-05-09T15:00:00",
                end_timezone="UTC",
                body="Review the new designs",
                attendees=["alice@example.com", "bob@example.com"],
                location="Room 101",
                is_online_meeting=True,
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["content"] == "Review the new designs"
        assert len(payload["attendees"]) == 2
        assert payload["attendees"][0]["emailAddress"]["address"] == "alice@example.com"
        assert payload["location"]["displayName"] == "Room 101"
        assert payload["isOnlineMeeting"] is True
        assert payload["onlineMeetingProvider"] == "teamsForBusiness"

    @respx.mock
    def test_create_calendar_event_all_day(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/events").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENT_ALLDAY)
        )
        with GraphClient("tok") as client:
            calendar.create_calendar_event(
                client,
                subject="Company Holiday",
                start_datetime="2026-05-25T00:00:00",
                start_timezone="UTC",
                end_datetime="2026-05-26T00:00:00",
                end_timezone="UTC",
                is_all_day=True,
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["isAllDay"] is True

    @respx.mock
    def test_check_availability(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/calendar/getSchedule").mock(
            return_value=httpx.Response(200, json=SAMPLE_SCHEDULE_RESPONSE)
        )
        with GraphClient("tok") as client:
            result = calendar.check_availability(
                client,
                schedules=["alice@example.com", "bob@example.com"],
                start_datetime="2026-05-08T09:00:00",
                start_timezone="UTC",
                end_datetime="2026-05-08T17:00:00",
                end_timezone="UTC",
            )

        assert len(result["value"]) == 2
        assert result["value"][0]["scheduleId"] == "alice@example.com"
        payload = json.loads(route.calls[0].request.content)
        assert payload["schedules"] == ["alice@example.com", "bob@example.com"]
        assert payload["availabilityViewInterval"] == 30

    @respx.mock
    def test_safe_id_encoding(self):
        event_id = "AAMk+test/id=with=special"
        encoded_id = "AAMk%2Btest%2Fid%3Dwith%3Dspecial"
        respx.get(f"{GRAPH_BASE_URL}/me/events/{encoded_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENT)
        )
        with GraphClient("tok") as client:
            event = calendar.get_calendar_event(client, event_id)

        assert event["subject"] == "Sprint Planning"


class TestCalendarAsync:
    """Async calendar operation tests."""

    @respx.mock
    async def test_alist_calendar_events(self):
        respx.get(f"{GRAPH_BASE_URL}/me/calendarView").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENTS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            events = await calendar.alist_calendar_events(
                client,
                start_datetime="2026-05-07T00:00:00Z",
                end_datetime="2026-05-14T00:00:00Z",
            )

        assert len(events) == 2
        assert events[0]["subject"] == "Sprint Planning"

    @respx.mock
    async def test_aget_calendar_event(self):
        event_id = "AAMkAGI2-event-001"
        respx.get(f"{GRAPH_BASE_URL}/me/events/{event_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENT)
        )
        async with AsyncGraphClient("tok") as client:
            event = await calendar.aget_calendar_event(client, event_id)

        assert event["subject"] == "Sprint Planning"

    @respx.mock
    async def test_acreate_calendar_event(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/events").mock(
            return_value=httpx.Response(200, json=SAMPLE_CREATED_EVENT)
        )
        async with AsyncGraphClient("tok") as client:
            result = await calendar.acreate_calendar_event(
                client,
                subject="Design Review",
                start_datetime="2026-05-09T14:00:00",
                start_timezone="America/Los_Angeles",
                end_datetime="2026-05-09T15:00:00",
                end_timezone="America/Los_Angeles",
                attendees=["alice@example.com"],
                is_online_meeting=True,
            )

        assert result["subject"] == "Design Review"
        payload = json.loads(route.calls[0].request.content)
        assert len(payload["attendees"]) == 1
        assert payload["isOnlineMeeting"] is True

    @respx.mock
    async def test_acheck_availability(self):
        respx.post(f"{GRAPH_BASE_URL}/me/calendar/getSchedule").mock(
            return_value=httpx.Response(200, json=SAMPLE_SCHEDULE_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await calendar.acheck_availability(
                client,
                schedules=["alice@example.com"],
                start_datetime="2026-05-08T09:00:00",
                start_timezone="UTC",
                end_datetime="2026-05-08T17:00:00",
                end_timezone="UTC",
            )

        assert result["value"][0]["scheduleId"] == "alice@example.com"
        assert result["value"][0]["availabilityView"] == "0000220000220000"
