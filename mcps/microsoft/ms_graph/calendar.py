"""
Calendar operations using the Microsoft Graph API.

All functions accept a GraphClient or AsyncGraphClient and return parsed dicts.
"""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .graph_client import GraphClient, AsyncGraphClient

logger = logging.getLogger(__name__)


def _safe_id(value: str) -> str:
    """URL-encode an ID to prevent path traversal in Graph API URLs."""
    return quote(value, safe="")


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------

def list_calendar_events(
    client: GraphClient,
    start_datetime: str,
    end_datetime: str,
    top: int = 25,
) -> List[Dict[str, Any]]:
    """List calendar events in a date range using calendarView."""
    data = client.get(
        "/me/calendarView",
        params={
            "startDateTime": start_datetime,
            "endDateTime": end_datetime,
            "$top": top,
            "$orderby": "start/dateTime",
            "$select": "id,subject,start,end,location,organizer,isAllDay,isCancelled,isOnlineMeeting,onlineMeetingUrl,recurrence,attendees,bodyPreview",
        },
    )
    return data.get("value", [])


def get_calendar_event(
    client: GraphClient,
    event_id: str,
) -> Dict[str, Any]:
    """Get full details of a calendar event by ID."""
    return client.get(f"/me/events/{_safe_id(event_id)}")


def create_calendar_event(
    client: GraphClient,
    subject: str,
    start_datetime: str,
    start_timezone: str,
    end_datetime: str,
    end_timezone: str,
    body: str = "",
    attendees: Optional[List[str]] = None,
    location: str = "",
    is_online_meeting: bool = False,
    is_all_day: bool = False,
) -> Dict[str, Any]:
    """Create a calendar event."""
    payload: Dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start_datetime, "timeZone": start_timezone},
        "end": {"dateTime": end_datetime, "timeZone": end_timezone},
    }
    if body:
        payload["body"] = {"contentType": "Text", "content": body}
    if attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": addr}, "type": "required"}
            for addr in attendees
        ]
    if location:
        payload["location"] = {"displayName": location}
    if is_online_meeting:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"
    if is_all_day:
        payload["isAllDay"] = True
    return client.post("/me/events", json_data=payload)


def check_availability(
    client: GraphClient,
    schedules: List[str],
    start_datetime: str,
    start_timezone: str,
    end_datetime: str,
    end_timezone: str,
    availability_view_interval: int = 30,
) -> Dict[str, Any]:
    """Check free/busy information for a list of users."""
    payload = {
        "schedules": schedules,
        "startTime": {"dateTime": start_datetime, "timeZone": start_timezone},
        "endTime": {"dateTime": end_datetime, "timeZone": end_timezone},
        "availabilityViewInterval": availability_view_interval,
    }
    return client.post("/me/calendar/getSchedule", json_data=payload)


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------

async def alist_calendar_events(
    client: AsyncGraphClient,
    start_datetime: str,
    end_datetime: str,
    top: int = 25,
) -> List[Dict[str, Any]]:
    """List calendar events in a date range using calendarView (async)."""
    data = await client.get(
        "/me/calendarView",
        params={
            "startDateTime": start_datetime,
            "endDateTime": end_datetime,
            "$top": top,
            "$orderby": "start/dateTime",
            "$select": "id,subject,start,end,location,organizer,isAllDay,isCancelled,isOnlineMeeting,onlineMeetingUrl,recurrence,attendees,bodyPreview",
        },
    )
    return data.get("value", [])


async def aget_calendar_event(
    client: AsyncGraphClient,
    event_id: str,
) -> Dict[str, Any]:
    """Get full details of a calendar event by ID (async)."""
    return await client.get(f"/me/events/{_safe_id(event_id)}")


async def acreate_calendar_event(
    client: AsyncGraphClient,
    subject: str,
    start_datetime: str,
    start_timezone: str,
    end_datetime: str,
    end_timezone: str,
    body: str = "",
    attendees: Optional[List[str]] = None,
    location: str = "",
    is_online_meeting: bool = False,
    is_all_day: bool = False,
) -> Dict[str, Any]:
    """Create a calendar event (async)."""
    payload: Dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start_datetime, "timeZone": start_timezone},
        "end": {"dateTime": end_datetime, "timeZone": end_timezone},
    }
    if body:
        payload["body"] = {"contentType": "Text", "content": body}
    if attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": addr}, "type": "required"}
            for addr in attendees
        ]
    if location:
        payload["location"] = {"displayName": location}
    if is_online_meeting:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"
    if is_all_day:
        payload["isAllDay"] = True
    return await client.post("/me/events", json_data=payload)


async def acheck_availability(
    client: AsyncGraphClient,
    schedules: List[str],
    start_datetime: str,
    start_timezone: str,
    end_datetime: str,
    end_timezone: str,
    availability_view_interval: int = 30,
) -> Dict[str, Any]:
    """Check free/busy information for a list of users (async)."""
    payload = {
        "schedules": schedules,
        "startTime": {"dateTime": start_datetime, "timeZone": start_timezone},
        "endTime": {"dateTime": end_datetime, "timeZone": end_timezone},
        "availabilityViewInterval": availability_view_interval,
    }
    return await client.post("/me/calendar/getSchedule", json_data=payload)
