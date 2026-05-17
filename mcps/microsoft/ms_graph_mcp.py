#!/usr/bin/env python3
"""
Microsoft Graph MCP Server for Bond AI.

Provides email, calendar, Teams, file, and Power BI tools. Graph tools use the
user's Microsoft Graph OAuth token; Power BI tools use a PBI-scoped token — both
passed by Bond AI's backend as Authorization: Bearer headers (via separate
connection entries in bond_mcp_config).

Run:
    fastmcp run ms_graph_mcp.py --transport streamable-http --port 5557

Tool summary (23 tools):
  Email     : get_user_profile, list_emails, read_email, send_email
  Calendar  : list_calendar_events, get_calendar_event, create_calendar_event, check_availability
  Teams     : list_teams, list_chats, read_teams_messages, send_teams_message, get_teams_activity
  Files     : list_sharepoint_sites, list_files, inspect_file, upload_file, copy_or_rename_file
  Power BI  : list_powerbi_workspaces, list_powerbi_content, query_dataset, refresh_dataset, export_report
"""

import logging
import os
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from ms_graph.auth import get_graph_token, get_powerbi_token
from ms_graph.graph_client import AsyncGraphClient
from ms_graph import mail as mail_ops
from ms_graph import calendar as calendar_ops
from ms_graph import teams as teams_ops
from ms_graph import files as files_ops
from ms_graph import power_bi as pbi_ops
from ms_graph.power_bi import AsyncPowerBIClient
from ms_graph.teams import TeamsNotAvailableError, extract_message_text, extract_message_sender

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app):
    """Warn if auth proxy is unreachable when local auth is configured."""
    if os.environ.get("MS_CLIENT_ID"):
        from auth import OAuthProxyClient
        proxy = OAuthProxyClient()
        try:
            proxy.check_proxy()
            logger.info("Auth proxy validated for local Microsoft auth")
        except RuntimeError as e:
            logger.warning("Auth proxy not available: %s", e)
    yield


mcp = FastMCP("Microsoft Graph MCP Server", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_user_profile() -> str:
    """
    Get the authenticated user's profile information.

    Returns the user's display name, email addresses, and account identifiers.
    Useful for discovering who you are sending email as.

    IMPORTANT: If a "Mailbox Address" is shown, use that as the from_address
    when sending email. This is the address that the mail server is authorized
    to send from, and avoids "via" warnings and spam filtering.
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        profile = await mail_ops.aget_profile(client)

    lines = [
        f"**Display Name:** {profile.get('displayName', '?')}",
        f"**Mail:** {profile.get('mail', '(not set)')}",
        f"**User Principal Name:** {profile.get('userPrincipalName', '?')}",
    ]
    mailbox_addr = profile.get("mailboxAddress")
    if mailbox_addr:
        lines.append(f"**Mailbox Address:** {mailbox_addr}")
    if profile.get("jobTitle"):
        lines.append(f"**Job Title:** {profile['jobTitle']}")
    lines.append(f"**ID:** `{profile.get('id', '?')}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_emails(folder: str = "inbox", query: str = "", top: int = 10) -> str:
    """
    List recent emails or search email messages.

    When query is empty, lists recent messages in the specified folder (default: inbox).
    When query is provided, searches across all folders using the given keyword query
    and the folder parameter is ignored.

    Args:
        folder: Mail folder to list from (default: inbox). Ignored when query is set.
        query: Search query (e.g., "from:alice budget report"). Empty to list without searching.
        top: Maximum number of messages to return (default: 10).
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if query:
            messages = await mail_ops.asearch_messages(client, query=query, top=top)
        else:
            messages = await mail_ops.alist_messages(client, folder=folder, top=top)

    if not messages:
        if query:
            return f'No messages found matching "{query}".'
        return "No messages found."

    if query:
        lines = [f'Found {len(messages)} result(s) for "{query}":\n']
    else:
        lines = [f"Found {len(messages)} message(s):\n"]

    for i, msg in enumerate(messages, 1):
        sender = msg.get("from", {}).get("emailAddress", {})
        lines.append(
            f"{i}. **{msg.get('subject', '(no subject)')}**\n"
            f"   From: {sender.get('name', '?')} <{sender.get('address', '?')}>\n"
            f"   Date: {msg.get('receivedDateTime', '?')}\n"
            f"   ID: `{msg.get('id', '?')}`"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def read_email(message_id: str) -> str:
    """
    Read a single email message by its ID.

    Args:
        message_id: The Graph API message ID (from list_emails output).
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        msg = await mail_ops.aget_message(client, message_id)

    sender = msg.get("from", {}).get("emailAddress", {})
    to_addrs = ", ".join(
        r.get("emailAddress", {}).get("address", "?")
        for r in msg.get("toRecipients", [])
    )
    body = msg.get("body", {})
    content = body.get("content", "")
    if body.get("contentType") != "text":
        content = f"[HTML content, {len(content)} chars]\n{content[:3000]}"

    return (
        f"**Subject:** {msg.get('subject', '(no subject)')}\n"
        f"**From:** {sender.get('name', '?')} <{sender.get('address', '?')}>\n"
        f"**To:** {to_addrs}\n"
        f"**Date:** {msg.get('receivedDateTime', '?')}\n\n"
        f"{content}"
    )


@mcp.tool()
async def send_email(to: str, subject: str, body: str, body_type: str = "auto", cc: str = "", from_address: str = "") -> str:
    """
    Send an email message.

    Args:
        to: Recipient email address (comma-separated for multiple).
        subject: Email subject line.
        body: Email body content. HTML is auto-detected via MIME sniffing — bodies
            containing HTML tags (e.g. <strong>, <a href="...">, <br>, <p>) are
            sent as HTML automatically. Use body_type to override.
        body_type: Content type of the body: "auto" (default, detect from content),
            "HTML" (always send as HTML), or "Text" (always send as plain text).
        cc: CC recipients (comma-separated, optional).
        from_address: Sender email address (optional). Use to send from a specific
            alias (e.g., your Outlook address instead of the account login address).
    """
    token = get_graph_token()
    to_list = [addr.strip() for addr in to.split(",") if addr.strip()]
    cc_list = [addr.strip() for addr in cc.split(",") if addr.strip()] if cc else None

    async with AsyncGraphClient(token) as client:
        await mail_ops.asend_message(
            client, to=to_list, subject=subject, body=body,
            cc=cc_list, from_address=from_address or None,
            body_type=body_type,
        )

    cc_note = f" (CC: {cc})" if cc else ""
    return f"Email sent to {to}{cc_note}."


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_calendar_events(
    start_date: str = "",
    end_date: str = "",
    top: int = 10,
) -> str:
    """
    List calendar events in a date range.

    Returns events from the user's primary calendar within the specified range.
    If no range is specified, defaults to the next 7 days.

    Args:
        start_date: Start date/time in ISO 8601 format (e.g., "2026-05-07T00:00:00Z").
                    Defaults to now.
        end_date: End date/time in ISO 8601 format (e.g., "2026-05-14T00:00:00Z").
                  Defaults to 7 days from start_date.
        top: Maximum number of events to return (default: 10).
    """
    from datetime import datetime, timedelta, timezone as tz

    if not start_date:
        now = datetime.now(tz.utc)
        start_date = now.isoformat()
    if not end_date:
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end_date = (start_dt + timedelta(days=7)).isoformat()

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        events = await calendar_ops.alist_calendar_events(
            client, start_datetime=start_date, end_datetime=end_date, top=top
        )

    if not events:
        return "No calendar events found in the specified date range."

    lines = [f"Found {len(events)} event(s):\n"]
    for i, event in enumerate(events, 1):
        subject = event.get("subject", "(no subject)")
        start = event.get("start", {})
        end = event.get("end", {})
        start_str = start.get("dateTime", "?")
        end_str = end.get("dateTime", "?")
        start_tz = start.get("timeZone", "")
        location = event.get("location", {}).get("displayName", "")
        organizer = event.get("organizer", {}).get("emailAddress", {}).get("name", "")
        is_all_day = event.get("isAllDay", False)
        is_cancelled = event.get("isCancelled", False)
        online_url = event.get("onlineMeetingUrl", "")

        time_str = "All day" if is_all_day else f"{start_str} - {end_str} ({start_tz})"
        status = " [CANCELLED]" if is_cancelled else ""

        entry = (
            f"{i}. **{subject}**{status}\n"
            f"   Time: {time_str}\n"
        )
        if organizer:
            entry += f"   Organizer: {organizer}\n"
        if location:
            entry += f"   Location: {location}\n"
        if online_url:
            entry += f"   Online: {online_url}\n"
        entry += f"   ID: `{event.get('id', '?')}`"
        lines.append(entry)

    return "\n\n".join(lines)


@mcp.tool()
async def get_calendar_event(event_id: str) -> str:
    """
    Get detailed information about a specific calendar event.

    Args:
        event_id: The event ID (from list_calendar_events output).
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        event = await calendar_ops.aget_calendar_event(client, event_id)

    subject = event.get("subject", "(no subject)")
    start = event.get("start", {})
    end = event.get("end", {})
    start_str = f"{start.get('dateTime', '?')} ({start.get('timeZone', '?')})"
    end_str = f"{end.get('dateTime', '?')} ({end.get('timeZone', '?')})"
    location = event.get("location", {}).get("displayName", "")
    organizer = event.get("organizer", {}).get("emailAddress", {})
    body = event.get("body", {})
    body_content = body.get("content", "")
    if body.get("contentType") != "text":
        body_content = f"[HTML content, {len(body_content)} chars]\n{body_content[:3000]}"

    attendees = event.get("attendees", [])
    attendee_lines = []
    for att in attendees:
        email = att.get("emailAddress", {})
        status = att.get("status", {}).get("response", "none")
        attendee_lines.append(f"  - {email.get('name', '?')} <{email.get('address', '?')}> ({status})")

    is_all_day = event.get("isAllDay", False)
    online_url = event.get("onlineMeetingUrl", "")
    recurrence = event.get("recurrence")

    lines = [
        f"**Subject:** {subject}",
        f"**Time:** {'All day' if is_all_day else f'{start_str} to {end_str}'}",
    ]
    if organizer:
        lines.append(f"**Organizer:** {organizer.get('name', '?')} <{organizer.get('address', '?')}>")
    if location:
        lines.append(f"**Location:** {location}")
    if online_url:
        lines.append(f"**Online Meeting:** {online_url}")
    if recurrence:
        pattern = recurrence.get("pattern", {})
        lines.append(f"**Recurrence:** {pattern.get('type', 'unknown')} (every {pattern.get('interval', 1)} {pattern.get('type', '')})")
    if attendee_lines:
        lines.append(f"**Attendees ({len(attendee_lines)}):**")
        lines.extend(attendee_lines)
    lines.append(f"**ID:** `{event.get('id', '?')}`")
    if body_content:
        lines.append(f"\n---\n{body_content}")

    return "\n".join(lines)


@mcp.tool()
async def create_calendar_event(
    subject: str,
    start_datetime: str,
    end_datetime: str,
    timezone: str = "UTC",
    attendees: str = "",
    location: str = "",
    body: str = "",
    is_online_meeting: bool = False,
    is_all_day: bool = False,
) -> str:
    """
    Create a new calendar event.

    Args:
        subject: Event title/subject.
        start_datetime: Start date and time in ISO 8601 format (e.g., "2026-05-08T10:00:00").
        end_datetime: End date and time in ISO 8601 format (e.g., "2026-05-08T11:00:00").
        timezone: IANA timezone for start/end times (e.g., "America/New_York", "UTC"). Default: UTC.
        attendees: Comma-separated list of attendee email addresses (optional).
        location: Event location (optional).
        body: Event description/body text (optional).
        is_online_meeting: Whether to create a Teams online meeting link (default: false).
        is_all_day: Whether this is an all-day event (default: false).
    """
    attendee_list = [addr.strip() for addr in attendees.split(",") if addr.strip()] if attendees else None

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        event = await calendar_ops.acreate_calendar_event(
            client,
            subject=subject,
            start_datetime=start_datetime,
            start_timezone=timezone,
            end_datetime=end_datetime,
            end_timezone=timezone,
            body=body,
            attendees=attendee_list,
            location=location,
            is_online_meeting=is_online_meeting,
            is_all_day=is_all_day,
        )

    result = f"Event '{subject}' created successfully."
    start = event.get("start", {})
    result += f"\nTime: {start.get('dateTime', '?')} ({start.get('timeZone', '?')})"
    if event.get("onlineMeetingUrl"):
        result += f"\nMeeting link: {event['onlineMeetingUrl']}"
    result += f"\nID: `{event.get('id', '?')}`"
    return result


@mcp.tool()
async def check_availability(
    emails: str,
    start_datetime: str,
    end_datetime: str,
    timezone: str = "UTC",
) -> str:
    """
    Check free/busy availability for one or more people.

    Useful for finding meeting times. Returns availability status for each
    person in the specified time range.

    Args:
        emails: Comma-separated email addresses to check availability for.
        start_datetime: Start of the time range in ISO 8601 format (e.g., "2026-05-08T09:00:00").
        end_datetime: End of the time range in ISO 8601 format (e.g., "2026-05-08T17:00:00").
        timezone: IANA timezone (e.g., "America/New_York", "UTC"). Default: UTC.
    """
    email_list = [addr.strip() for addr in emails.split(",") if addr.strip()]
    if not email_list:
        return "No email addresses provided."

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        result = await calendar_ops.acheck_availability(
            client,
            schedules=email_list,
            start_datetime=start_datetime,
            start_timezone=timezone,
            end_datetime=end_datetime,
            end_timezone=timezone,
        )

    schedules = result.get("value", [])
    if not schedules:
        return "No availability information returned."

    lines = [f"Availability for {len(schedules)} schedule(s):\n"]
    for sched in schedules:
        email = sched.get("scheduleId", "?")
        avail_view = sched.get("availabilityView", "")
        schedule_items = sched.get("scheduleItems", [])

        free_count = avail_view.count("0")
        total_slots = len(avail_view)
        if total_slots > 0:
            free_pct = int((free_count / total_slots) * 100)
            summary = f"{free_pct}% free ({free_count}/{total_slots} slots)"
        else:
            summary = "No slots"

        entry = f"**{email}** — {summary}"
        if schedule_items:
            entry += "\n   Busy times:"
            for item in schedule_items[:10]:
                item_subject = item.get("subject", "(private)")
                item_start = item.get("start", {}).get("dateTime", "?")
                item_end = item.get("end", {}).get("dateTime", "?")
                item_status = item.get("status", "?")
                entry += f"\n   - {item_start} to {item_end}: {item_subject} ({item_status})"
        lines.append(entry)

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_teams(team_id: str = "") -> str:
    """
    List joined Microsoft Teams, or list channels within a specific team.

    When team_id is empty, returns all teams the user has joined.
    When team_id is provided, returns the channels within that team.

    Args:
        team_id: Team ID to list channels for (from a previous call with no team_id).
                 Leave empty to list all joined teams.
    """
    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            if team_id:
                channels = await teams_ops.alist_channels(client, team_id)
                if not channels:
                    return "No channels found."
                lines = [f"Found {len(channels)} channel(s) in team `{team_id}`:\n"]
                for ch in channels:
                    lines.append(f"- **{ch.get('displayName', '?')}** (ID: `{ch.get('id', '?')}`)")
                return "\n".join(lines)
            else:
                team_list = await teams_ops.alist_joined_teams(client)
                if not team_list:
                    return "No teams found."
                lines = [f"Joined {len(team_list)} team(s):\n"]
                for t in team_list:
                    lines.append(f"- **{t.get('displayName', '?')}** (ID: `{t.get('id', '?')}`)")
                return "\n".join(lines)
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account. A Microsoft 365 license is required."


@mcp.tool()
async def list_chats(chat_type: str = "", top: int = 20) -> str:
    """
    List Teams chats (1:1, group, meeting) with last message preview.

    Args:
        chat_type: Filter by type: oneOnOne, group, or meeting. Empty for all.
        top: Maximum number of chats to return (default: 20).
    """
    valid_types = {"", "oneOnOne", "group", "meeting"}
    if chat_type not in valid_types:
        return f"Invalid chat_type: {chat_type}. Must be one of: oneOnOne, group, meeting (or empty for all)."

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            chats = await teams_ops.alist_chats(client, chat_type=chat_type, top=top)
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."

    if not chats:
        return "No chats found."

    lines = [f"Found {len(chats)} chat(s):\n"]
    for i, chat in enumerate(chats, 1):
        ct = chat.get("chatType", "?")
        topic = chat.get("topic")
        members = chat.get("members") or []
        member_names = [m.get("displayName", "?") for m in members if m.get("displayName")]
        members_str = ", ".join(member_names[:5])
        if len(member_names) > 5:
            members_str += f" (+{len(member_names) - 5} more)"

        preview = chat.get("lastMessagePreview") or {}
        preview_text = (preview.get("body") or {}).get("content", "")
        preview_sender = ((preview.get("from") or {}).get("user") or {}).get("displayName", "")
        preview_date = preview.get("createdDateTime", "")

        label = topic or members_str or "(unnamed)"
        lines.append(
            f"{i}. **{label}** (type: {ct})\n"
            f"   Members: {members_str or '(unknown)'}\n"
            f"   Last: {preview_sender}: {preview_text[:100]}" + (f" ({preview_date})" if preview_date else "") + "\n"
            f"   ID: `{chat.get('id', '?')}`"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def read_teams_messages(team_id: str = "", channel_id: str = "", chat_id: str = "", top: int = 20) -> str:
    """
    Read recent messages from a Teams channel or chat.

    Provide either:
    - chat_id to read from a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to read from a team channel (from list_teams)

    Args:
        team_id: Team ID (from list_teams with no team_id). Required for channel reading.
        channel_id: Channel ID (from list_teams with team_id). Required for channel reading.
        chat_id: Chat ID (from list_chats). Use this for 1:1 and group chats.
        top: Maximum number of messages to return (default: 20).
    """
    if not chat_id and not (team_id and channel_id):
        return "Provide either chat_id, or both team_id and channel_id."

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            if chat_id:
                messages = await teams_ops.alist_chat_messages(client, chat_id, top=top)
                source = f"chat `{chat_id}`"
            else:
                messages = await teams_ops.alist_channel_messages(client, team_id, channel_id, top=top)
                source = f"channel `{channel_id}`"
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."

    if not messages:
        return f"No messages found in {source}."

    lines = [f"Found {len(messages)} message(s) in {source}:\n"]
    for i, msg in enumerate(messages, 1):
        sender = extract_message_sender(msg)
        content = extract_message_text(msg)
        lines.append(
            f"{i}. **{sender}** ({msg.get('createdDateTime', '?')})\n"
            f"   {content or '(empty)'}\n"
            f"   ID: `{msg.get('id', '?')}`"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def send_teams_message(message: str, team_id: str = "", channel_id: str = "", chat_id: str = "") -> str:
    """
    Send a message to a Teams channel or chat.

    Provide either:
    - chat_id to send to a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to send to a team channel (from list_teams)

    Args:
        message: Message content to send.
        team_id: Team ID (from list_teams with no team_id). Required for channel sending.
        channel_id: Channel ID (from list_teams with team_id). Required for channel sending.
        chat_id: Chat ID (from list_chats). Use this for 1:1 and group chats.
    """
    if not chat_id and not (team_id and channel_id):
        return "Provide either chat_id, or both team_id and channel_id."

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            if chat_id:
                await teams_ops.asend_chat_message(client, chat_id, message)
                return "Message sent to Teams chat."
            else:
                await teams_ops.asend_channel_message(client, team_id, channel_id, message)
                return "Message sent to Teams channel."
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."


@mcp.tool()
async def get_teams_activity(hours: int = 24) -> str:
    """
    Get recent Teams activity across all channels and chats as a CSV digest.

    Scans joined teams' channels and recent chats for messages within the
    specified time window. Ideal for catching up on what you missed.

    Args:
        hours: Look back this many hours (default: 24).
    """
    import csv
    import io

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            activity = await teams_ops.aget_teams_activity(client, hours=hours)
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."

    if not activity:
        return f"No Teams activity in the last {hours} hours."

    sources = {row["source_name"] for row in activity}
    output = io.StringIO()
    output.write(
        f"Activity in the last {hours} hours: "
        f"{len(activity)} messages across {len(sources)} sources\n\n"
    )
    writer = csv.writer(output)
    writer.writerow(["source", "source_name", "sender", "timestamp", "preview"])
    for row in activity:
        writer.writerow([
            row["source"],
            row["source_name"],
            row["sender"],
            row["timestamp"],
            row["preview"],
        ])
    return output.getvalue()


# ---------------------------------------------------------------------------
# Files / OneDrive / SharePoint
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    """Format a file size in bytes to a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _format_drive_item(item: dict) -> str:
    """Format a driveItem as a single markdown line."""
    name = item.get("name", "?")
    item_id = item.get("id", "?")
    if "folder" in item:
        child_count = item["folder"].get("childCount", "?")
        return f"- **{name}/** ({child_count} items) — ID: `{item_id}`"
    mime = item.get("file", {}).get("mimeType", "")
    size = _format_size(item.get("size", 0))
    return f"- **{name}** ({mime}, {size}) — ID: `{item_id}`"


@mcp.tool()
async def list_sharepoint_sites(query: str = "", top: int = 10) -> str:
    """
    Search for SharePoint sites, or list followed sites.

    Args:
        query: Search query to find sites (e.g., "engineering"). Leave empty to list followed sites.
        top: Maximum number of results (default: 10).
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        sites = await files_ops.alist_sites(client, query=query, top=top)

    if not sites:
        if query:
            return f'No SharePoint sites found matching "{query}".'
        return "No followed SharePoint sites found."

    desc = f'matching "{query}"' if query else "followed"
    lines = [f"Found {len(sites)} {desc} site(s):\n"]
    for site in sites:
        name = site.get("displayName", site.get("name", "?"))
        site_id = site.get("id", "?")
        web_url = site.get("webUrl", "")
        lines.append(f"- **{name}** (ID: `{site_id}`)")
        if web_url:
            lines.append(f"  {web_url}")
    return "\n".join(lines)


@mcp.tool()
async def list_files(folder_path: str = "", site_id: str = "", query: str = "", top: int = 20) -> str:
    """
    List or search files in OneDrive or SharePoint.

    Three modes depending on the parameters provided:
    - query set: searches across all drives using Microsoft Search (folder_path ignored)
    - site_id set (no query): lists files in a SharePoint site's document library
    - neither set: lists files in the user's OneDrive

    Args:
        folder_path: Folder path to browse (e.g., "Documents/Reports"). Empty for root.
                     Ignored when query is provided.
        site_id: SharePoint site ID (from list_sharepoint_sites). Empty for OneDrive.
                 Ignored when query is provided.
        query: Search query (e.g., "Q4 budget"). When set, searches across all drives.
        top: Maximum number of items to return (default: 20).
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if query:
            results = await files_ops.asearch_files_unified(client, query=query, top=top)
            if not results:
                return f'No files found matching "{query}".'
            lines = [f'Found {len(results)} result(s) for "{query}":\n']
            for i, item in enumerate(results, 1):
                name = item.get("name", "?")
                web_url = item.get("webUrl", "")
                summary = item.get("_searchSummary", "")
                size = _format_size(item.get("size", 0))
                lines.append(
                    f"{i}. **{name}** ({size})\n"
                    f"   ID: `{item.get('id', '?')}`"
                )
                if summary:
                    lines.append(f"   Summary: {summary}")
                if web_url:
                    lines.append(f"   URL: {web_url}")
            return "\n\n".join(lines)
        else:
            items = await files_ops.alist_drive_children(
                client, folder_path=folder_path, site_id=site_id, top=top
            )
            if not items:
                loc = f' in "{folder_path}"' if folder_path else " in root"
                return f"No files found{loc}."
            loc = f'"{folder_path}"' if folder_path else "root"
            lines = [f"Found {len(items)} item(s) in {loc}:\n"]
            for item in items:
                lines.append(_format_drive_item(item))
            return "\n".join(lines)


@mcp.tool()
async def inspect_file(item_id: str, site_id: str = "", read_content: bool = False) -> str:
    """
    Get metadata and optionally the content of a file from OneDrive or SharePoint.

    By default returns metadata only (name, type, size, modified date, ID, URL).
    Pass read_content=True to also download and return the file's text content
    (up to 512 KB). Binary files always return metadata and a browser link regardless.

    Args:
        item_id: The drive item ID (from list_files output).
        site_id: SharePoint site ID. Leave empty for OneDrive.
        read_content: False (default) to return metadata only.
                      True to download and return text content.
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if read_content:
            item, content = await files_ops.aget_drive_item_content(client, item_id, site_id=site_id)
        else:
            item = await files_ops.aget_drive_item(client, item_id, site_id=site_id)
            content = None

    name = item.get("name", "?")
    modified = item.get("lastModifiedDateTime", "?")
    modified_by = item.get("lastModifiedBy", {}).get("user", {}).get("displayName", "?")
    web_url = item.get("webUrl", "")

    if "folder" in item:
        type_line = f"**Type:** Folder ({item['folder'].get('childCount', '?')} items)"
    else:
        mime = item.get("file", {}).get("mimeType", "unknown")
        size = _format_size(item.get("size", 0))
        type_line = f"**Type:** {mime} ({size})"

    header = (
        f"**Name:** {name}\n"
        f"{type_line}\n"
        f"**Modified:** {modified} by {modified_by}\n"
        f"**ID:** `{item.get('id', '?')}`"
    )
    if web_url:
        header += f"\n**URL:** {web_url}"

    if not read_content:
        return header

    if content is not None:
        return f"{header}\n\n---\n{content}"

    if "folder" in item:
        msg = "This is a folder, not a file. Use list_files to browse its contents."
    elif item.get("size", 0) > files_ops.MAX_TEXT_DOWNLOAD_BYTES:
        msg = "This file is too large to display as text (limit: 512 KB)."
    else:
        msg = "This is a binary file and cannot be displayed as text."
    if web_url:
        msg += f"\nOpen in browser: {web_url}"
    return f"{header}\n\n{msg}"


@mcp.tool()
async def upload_file(
    filename: str,
    content: str,
    folder_path: str = "",
    site_id: str = "",
) -> str:
    """
    Create or overwrite a text file in OneDrive or SharePoint.

    Uses the simple upload endpoint (max 4 MB). The file is created if it does
    not exist, or overwritten if it does. Supported text formats: .txt, .md,
    .html, .csv, .json, .xml, .yaml. Binary file formats are not supported by
    this tool — use copy_or_rename_file with action="copy" to duplicate an
    existing binary file instead.

    Args:
        filename: File name including extension (e.g. "report.md", "data.csv").
        content: Text content to write to the file.
        folder_path: Destination folder path (e.g. "Documents" or
            "Shared Documents/Templates"). Empty string uploads to the drive root.
        site_id: SharePoint site ID (from list_sharepoint_sites). Empty for OneDrive.
    """
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        item = await files_ops.aupload_file(
            client,
            folder_path=folder_path,
            filename=filename,
            content=content,
            site_id=site_id,
        )

    web_url = item.get("webUrl", "")
    item_id = item.get("id", "?")
    size = item.get("size", 0)
    result = f"File '{filename}' uploaded successfully."
    result += f"\nID: `{item_id}`"
    result += f"\nSize: {_format_size(size)}"
    if web_url:
        result += f"\nURL: {web_url}"
    return result


@mcp.tool()
async def copy_or_rename_file(
    item_id: str,
    new_name: str,
    action: str = "rename",
    destination_folder_id: str = "",
    site_id: str = "",
    destination_drive_id: str = "",
) -> str:
    """
    Copy or rename a file or folder.

    Set action to "rename" (default) to rename a file or folder in-place.
    Set action to "copy" to create a server-side copy with a new name — works
    for any file type including Word, Excel, and PDF. Useful for creating a
    new document from a template.

    Args:
        item_id: Drive item ID of the file or folder to act on (from list_files).
        new_name: New name including extension (e.g. "Final-Report.docx").
        action: "rename" (default) or "copy".
        destination_folder_id: For copy only — item ID of the destination folder.
            Leave empty to copy into the same folder as the source.
        site_id: SharePoint site ID of the source item. Empty for OneDrive.
        destination_drive_id: For copy only — drive ID of the destination drive.
            Leave empty to copy within the same drive.
    """
    action = action.lower()
    if action not in ("rename", "copy"):
        return f"Invalid action '{action}'. Must be 'rename' or 'copy'."

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if action == "copy":
            status = await files_ops.acopy_drive_item(
                client,
                item_id=item_id,
                new_name=new_name,
                destination_folder_id=destination_folder_id,
                site_id=site_id,
                destination_drive_id=destination_drive_id,
            )
            resource_id = status.get("resourceId", "?")
            return f"File copied successfully as '{new_name}'.\nNew item ID: `{resource_id}`"
        else:
            item = await files_ops.arename_drive_item(
                client,
                item_id=item_id,
                new_name=new_name,
                site_id=site_id,
            )
            web_url = item.get("webUrl", "")
            result = f"Renamed to '{item.get('name', new_name)}' successfully."
            result += f"\nID: `{item.get('id', item_id)}`"
            if web_url:
                result += f"\nURL: {web_url}"
            return result


# ---------------------------------------------------------------------------
# Power BI
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_powerbi_workspaces() -> str:
    """
    List all Power BI workspaces the user has access to.

    Workspaces contain datasets, reports, and dashboards. Use the workspace ID
    with other Power BI tools to list content or run queries.
    """
    token = get_powerbi_token()
    async with AsyncPowerBIClient(token) as client:
        workspaces = await pbi_ops.alist_workspaces(client)

    # Always prepend My workspace — it exists for every user but has no group ID
    lines = [f"Found {len(workspaces) + 1} workspace(s):\n"]
    lines.append("- **My workspace** (ID: `me`)")
    for ws in workspaces:
        capacity = " [Premium]" if ws.get("isOnDedicatedCapacity") else ""
        lines.append(f"- **{ws.get('name', '?')}**{capacity} (ID: `{ws.get('id', '?')}`)")
    return "\n".join(lines)


@mcp.tool()
async def list_powerbi_content(workspace_id: str, content_type: str = "all") -> str:
    """
    List datasets, reports, and/or dashboards in a Power BI workspace.

    Args:
        workspace_id: The workspace ID (from list_powerbi_workspaces). Use "me" for My workspace.
        content_type: What to list: "datasets", "reports", "dashboards", or "all" (default).
    """
    content_type = content_type.lower()
    if content_type not in ("datasets", "reports", "dashboards", "all"):
        return f"Invalid content_type '{content_type}'. Must be: datasets, reports, dashboards, or all."

    ws = "" if workspace_id.lower() == "me" else workspace_id
    token = get_powerbi_token()
    async with AsyncPowerBIClient(token) as client:
        datasets = await pbi_ops.alist_datasets(client, ws) if content_type in ("datasets", "all") else []
        reports = await pbi_ops.alist_reports(client, ws) if content_type in ("reports", "all") else []
        dashboards = await pbi_ops.alist_dashboards(client, ws) if content_type in ("dashboards", "all") else []

    lines = []
    if datasets:
        lines.append(f"**Datasets** ({len(datasets)}):")
        for ds in datasets:
            refreshable = " [refreshable]" if ds.get("isRefreshable") else ""
            lines.append(f"  - **{ds.get('name', '?')}**{refreshable} (ID: `{ds.get('id', '?')}`)")
    if reports:
        if lines:
            lines.append("")
        lines.append(f"**Reports** ({len(reports)}):")
        for r in reports:
            lines.append(f"  - **{r.get('name', '?')}** (ID: `{r.get('id', '?')}`, dataset: `{r.get('datasetId', '?')}`)")
    if dashboards:
        if lines:
            lines.append("")
        lines.append(f"**Dashboards** ({len(dashboards)}):")
        for d in dashboards:
            lines.append(f"  - **{d.get('displayName', '?')}** (ID: `{d.get('id', '?')}`)")

    if not lines:
        return f"No content found in workspace `{workspace_id}`."
    return "\n".join(lines)


@mcp.tool()
async def query_dataset(workspace_id: str, dataset_id: str, dax_query: str) -> str:
    """
    Execute a DAX query against a Power BI dataset and return results as CSV.

    The dataset must be on Premium or Fabric capacity and you must have Build
    permission on the dataset.

    Args:
        workspace_id: The workspace ID (from list_powerbi_workspaces). Use "me" for My workspace.
        dataset_id: The dataset ID (from list_powerbi_content).
        dax_query: A valid DAX query (e.g., "EVALUATE TOPN(10, 'Sales', 'Sales'[Amount], DESC)").
    """
    ws = "" if workspace_id.lower() == "me" else workspace_id
    token = get_powerbi_token()
    async with AsyncPowerBIClient(token) as client:
        result = await pbi_ops.aexecute_dax_query(client, ws, dataset_id, dax_query)

    csv_output = pbi_ops._format_dax_results(result)
    row_count = len(result.get("results", [{}])[0].get("tables", [{}])[0].get("rows", []))
    return f"Query returned {row_count} row(s):\n\n{csv_output}"


@mcp.tool()
async def refresh_dataset(workspace_id: str, dataset_id: str) -> str:
    """
    Trigger an on-demand refresh of a Power BI dataset.

    Starts the refresh and returns immediately — the refresh runs in the background.
    Use list_powerbi_content to find refreshable datasets (marked [refreshable]).

    Args:
        workspace_id: The workspace ID (from list_powerbi_workspaces).
        dataset_id: The dataset ID (from list_powerbi_content).
    """
    ws = "" if workspace_id.lower() == "me" else workspace_id
    token = get_powerbi_token()
    async with AsyncPowerBIClient(token) as client:
        await pbi_ops.atrigger_refresh(client, ws, dataset_id)

    return f"Refresh triggered for dataset `{dataset_id}`. The refresh runs in the background."


@mcp.tool()
async def export_report(
    workspace_id: str,
    report_id: str,
    export_format: str = "PDF",
    pages: str = "",
    folder_path: str = "Power BI Exports",
) -> str:
    """
    Export a Power BI report to PDF, PNG, or PPTX and save it to OneDrive.

    Exports the report, downloads the file, and uploads it to the user's OneDrive
    so it can be shared or attached to other workflows. Requires the workspace to
    be on Premium or Fabric capacity.

    Args:
        workspace_id: The workspace ID (from list_powerbi_workspaces).
        report_id: The report ID (from list_powerbi_content).
        export_format: "PDF" (default), "PNG", or "PPTX".
        pages: Comma-separated page names to export (e.g., "ReportSection1,ReportSection2").
               Leave empty to export all pages.
        folder_path: OneDrive folder to save the export to (default: "Power BI Exports").
    """
    _mime_types = {
        "PDF": "application/pdf",
        "PNG": "image/png",
        "PPTX": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }

    export_format = export_format.upper()
    if export_format not in _mime_types:
        return f"Invalid export_format '{export_format}'. Must be: PDF, PNG, or PPTX."

    page_list = [p.strip() for p in pages.split(",") if p.strip()] if pages else None
    ws = "" if workspace_id.lower() == "me" else workspace_id

    # Step 1: Export from Power BI (uses PBI token)
    pbi_token = get_powerbi_token()
    async with AsyncPowerBIClient(pbi_token) as pbi_client:
        export_id = await pbi_ops.astart_export(
            pbi_client, ws, report_id, export_format, pages=page_list
        )
        status = await pbi_ops.apoll_export(pbi_client, ws, report_id, export_id)
        file_bytes = await pbi_ops.adownload_export(pbi_client, ws, report_id, export_id)

    ext = status.get("resourceFileExtension", f".{export_format.lower()}")
    filename = f"report-{report_id}{ext}"
    content_type = _mime_types[export_format]
    size = _format_size(len(file_bytes))

    # Step 2: Upload to OneDrive (uses Graph token).
    # If the Microsoft connection is not active, degrade gracefully rather than
    # raising mid-flight after the export bytes have already been downloaded.
    try:
        graph_token = get_graph_token()
        async with AsyncGraphClient(graph_token) as graph_client:
            item = await files_ops.aupload_bytes(
                graph_client,
                folder_path=folder_path,
                filename=filename,
                data=file_bytes,
                content_type=content_type,
            )
        web_url = item.get("webUrl", "")
        result = f"Report exported as {export_format} ({size}) and saved to OneDrive."
        result += f"\nFilename: {filename}"
        result += f"\nOneDrive folder: {folder_path}"
        if web_url:
            result += f"\nURL: {web_url}"
    except PermissionError:
        result = (
            f"Report exported as {export_format} ({size}), but could not save to OneDrive "
            f"because the Microsoft connection is not active. "
            f"Connect your Microsoft account in Bond AI Settings → Connections to enable OneDrive upload."
        )
    return result


if __name__ == "__main__":
    mcp.run()
