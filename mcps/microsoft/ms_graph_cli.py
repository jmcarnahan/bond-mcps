#!/usr/bin/env python3
"""
Microsoft Graph CLI -- test all ms_graph_mcp tools from the command line.

Mirrors the 23 consolidated MCP tools exactly so you can test them locally
with the shared auth proxy already running.

Usage:
    export MS_CLIENT_ID=<your-azure-app-client-id>

    # Profile
    ms-graph-cli whoami

    # Email
    ms-graph-cli email list [--folder inbox] [--top 10]
    ms-graph-cli email list --query "budget report" [--top 10]
    ms-graph-cli email read <message_id>
    ms-graph-cli email attachment <message_id> <attachment_id> [--out PATH]
    ms-graph-cli email send <to> <subject> <body> [--from <address>] [--cc <address>] [--attach PATH ...]

    # Calendar
    ms-graph-cli calendar list [--start <iso8601>] [--end <iso8601>] [--top 10]
    ms-graph-cli calendar get <event_id>
    ms-graph-cli calendar create <subject> <start> <end> [--timezone UTC] [--attendees a@x,b@x] [--location Room] [--online]
    ms-graph-cli calendar availability <emails> <start> <end> [--timezone UTC]

    # Teams
    ms-graph-cli teams list
    ms-graph-cli teams list --team-id <team_id>          # lists channels
    ms-graph-cli teams chats [--type oneOnOne|group|meeting] [--top 20]
    ms-graph-cli teams read --chat-id <chat_id> [--top 20]
    ms-graph-cli teams read --team-id <team_id> --channel-id <channel_id> [--top 20]
    ms-graph-cli teams attachment <message_id> <attachment_id> --chat-id <chat_id> [--out PATH]
    ms-graph-cli teams attachment <message_id> <attachment_id> --team-id <team_id> --channel-id <channel_id> [--out PATH]
    ms-graph-cli teams send <message> --chat-id <chat_id> [--attach PATH ...] [--image PATH ...]
    ms-graph-cli teams send <message> --team-id <team_id> --channel-id <channel_id> [--attach PATH ...] [--image PATH ...]
    ms-graph-cli teams activity [--hours 24]

    # Files
    ms-graph-cli files sites [--query <q>] [--top 10]
    ms-graph-cli files list [--path <folder>] [--site-id <id>] [--query <q>] [--top 20]
    ms-graph-cli files inspect <item_id_or_url> [--site-id <id>] [--content]
    ms-graph-cli files resolve <sharing_url> [--content]
    ms-graph-cli files upload <filename> <content> [--folder <path>] [--site-id <id>]
    ms-graph-cli files copy <item_id> <new_name> [--dest-folder <id>] [--site-id <id>] [--dest-drive <id>]
    ms-graph-cli files rename <item_id> <new_name> [--site-id <id>]

    # Power BI
    ms-graph-cli powerbi workspaces
    ms-graph-cli powerbi content <workspace_id> [--type datasets|reports|dashboards|all]
    ms-graph-cli powerbi query <workspace_id> <dataset_id> <dax_query>
    ms-graph-cli powerbi refresh <workspace_id> <dataset_id> [--history] [--top 5]
    ms-graph-cli powerbi export <workspace_id> <report_id> [--format PDF|PNG|PPTX] [--pages <p1,p2>] [--out <file>]
"""

import argparse
import mimetypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from auth import log_discipline  # noqa: E402

log_discipline.apply()

from ms_graph import attachments as attachment_ops
from ms_graph import calendar, files, mail, teams
from ms_graph import power_bi as pbi_ops
from ms_graph.graph_client import GraphClient
from ms_graph.local_auth import get_local_powerbi_token, get_local_token
from ms_graph.power_bi import PowerBIClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def _fmt_message(msg: dict) -> str:
    sender = msg.get("from", {}).get("emailAddress", {})
    return (
        f"  ID:      {msg.get('id', '?')[:30]}...\n"
        f"  From:    {sender.get('name', '?')} <{sender.get('address', '?')}>\n"
        f"  Subject: {msg.get('subject', '(no subject)')}\n"
        f"  Date:    {msg.get('receivedDateTime', '?')}"
    )


def _fmt_drive_item(item: dict) -> str:
    name = item.get("name", "?")
    item_id = item.get("id", "?")
    if "folder" in item:
        return f"  {name}/  ({item['folder'].get('childCount', '?')} items)  [id: {item_id}]"
    mime = item.get("file", {}).get("mimeType", "")
    return f"  {name}  ({mime}, {_fmt_size(item.get('size', 0))})  [id: {item_id}]"


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def cmd_whoami(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        profile = mail.get_profile(client)

    print(f"Display Name:       {profile.get('displayName', '?')}")
    print(f"Mail:               {profile.get('mail', '(not set)')}")
    print(f"User Principal:     {profile.get('userPrincipalName', '?')}")
    if profile.get("mailboxAddress"):
        print(f"Mailbox Address:    {profile['mailboxAddress']}")
    if profile.get("jobTitle"):
        print(f"Job Title:          {profile['jobTitle']}")
    print(f"ID:                 {profile.get('id', '?')}")


def cmd_powerbi_whoami(args: argparse.Namespace) -> None:
    import base64
    import json

    token = get_local_powerbi_token()

    # Decode the JWT payload (no verification — just inspect claims)
    parts = token.split(".")
    if len(parts) >= 2:
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            aud = claims.get("aud", "?")
            scp = claims.get("scp", claims.get("roles", "?"))
            print(f"Token audience:     {aud}")
            print(f"Token scopes:       {scp}")
        except Exception:
            pass

    with PowerBIClient(token) as client:
        workspaces = pbi_ops.list_workspaces(client)

    print("Power BI token valid.")
    print(f"Workspaces accessible: {len(workspaces) + 1} (including My workspace)")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def cmd_email_list(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        if args.query:
            messages = mail.search_messages(client, query=args.query, top=args.top)
            header = f"Search results for '{args.query}'"
        else:
            messages = mail.list_messages(client, folder=args.folder, top=args.top)
            header = f"Recent messages in '{args.folder}'"

    if not messages:
        print("No messages found.")
        return

    print(f"{header} ({len(messages)}):\n")
    for i, msg in enumerate(messages, 1):
        print(f"[{i}]")
        print(_fmt_message(msg))
        print()


def _fmt_size(size_bytes: int) -> str:
    """Human-readable byte count. The CLI keeps its own copy rather than
    importing ms_graph_mcp, which would drag the whole server in."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _print_attachments(attachments: list[dict]) -> None:
    """Print one line per attachment, mirroring read_email's markdown section."""
    print("\nAttachments:")
    for att in attachments:
        summary = attachment_ops.attachment_summary(att)
        if summary["kind"] == "reference":
            detail = f"link: {summary['source_url'] or '(no URL)'}"
        else:
            parts = [summary["content_type"] or "unknown type", _fmt_size(summary["size"])]
            if summary["is_inline"]:
                parts.append("inline")
            detail = ", ".join(parts)
        print(f"  {summary['name'] or '(unnamed)'}  ({detail})  id: {summary['id']}")


def cmd_email_read(args: argparse.Namespace) -> None:
    token = get_local_token()
    attachments: list[dict] = []
    with GraphClient(token) as client:
        msg = mail.get_message(client, args.message_id)
        if msg.get("hasAttachments"):
            attachments = attachment_ops.list_message_attachments(client, args.message_id)

    sender = msg.get("from", {}).get("emailAddress", {})
    to_addrs = ", ".join(
        r.get("emailAddress", {}).get("address", "?") for r in msg.get("toRecipients", [])
    )
    print(f"From:    {sender.get('name', '?')} <{sender.get('address', '?')}>")
    print(f"Subject: {msg.get('subject', '(no subject)')}")
    print(f"Date:    {msg.get('receivedDateTime', '?')}")
    print(f"To:      {to_addrs}")
    print()
    body = msg.get("body", {})
    if body.get("contentType") == "text":
        print(body.get("content", ""))
    else:
        content = body.get("content", "")
        print(f"[HTML body — {len(content)} chars]\n")
        print(content[:3000])

    if attachments:
        _print_attachments(attachments)


def cmd_email_attachment(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        meta = attachment_ops.get_attachment_metadata(client, args.message_id, args.attachment_id)
        summary = attachment_ops.attachment_summary(meta)
        if summary["kind"] == "reference":
            print(f"Link attachment: {summary['source_url'] or '(no URL)'}")
            return
        data, ctype = attachment_ops.get_attachment_bytes(
            client, args.message_id, args.attachment_id
        )

    out = args.out or _default_attachment_filename(summary["name"], summary["kind"])
    Path(out).write_bytes(data)
    print(f"Saved {len(data)} bytes to {out} ({ctype})")


def _default_attachment_filename(name: str, kind: str) -> str:
    """The file name used when --out is not given.

    The attachment name comes from the sender, so only its last path segment
    is kept: a name like "../.ssh/authorized_keys" must not escape the
    current directory. Attached messages get a .eml suffix when they have none.
    """
    base = Path(name or "").name
    if base in ("", ".", ".."):
        base = "attachment"
    if kind == "item" and not Path(base).suffix:
        base = f"{base}.eml"
    return base


def cmd_email_send(args: argparse.Namespace) -> None:
    token = get_local_token()
    from_addr = args.from_address or os.environ.get("MS_DEFAULT_FROM_ADDRESS") or None
    cc_list = [a.strip() for a in args.cc.split(",") if a.strip()] if args.cc else None

    attachments = []
    for path in args.attach:
        source = Path(path)
        if not source.is_file():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        attachments.append(
            attachment_ops.ResolvedAttachment(
                name=source.name,
                data=source.read_bytes(),
                content_type=attachment_ops.guess_content_type(source.name),
            )
        )

    with GraphClient(token) as client:
        mail.send_message(
            client,
            to=[args.to],
            subject=args.subject,
            body=args.body,
            cc=cc_list,
            from_address=from_addr,
            attachments=attachments or None,
        )
    cc_note = f" (CC: {args.cc})" if args.cc else ""
    attach_note = f" with {len(attachments)} attachment(s)" if attachments else ""
    print(f"Email sent to {args.to}{cc_note}{attach_note}.")


# ---------------------------------------------------------------------------
# Inbox rules
# ---------------------------------------------------------------------------


def _fmt_rule(rule: dict) -> str:
    conditions = ", ".join(rule.get("conditions", {}).keys()) or "(none)"
    actions = ", ".join(rule.get("actions", {}).keys()) or "(none)"
    return (
        f"  [{rule.get('sequence', '?')}] {rule.get('displayName', '(no name)')}"
        f"  (enabled={rule.get('isEnabled', '?')}, readOnly={rule.get('isReadOnly', False)})\n"
        f"      id:         {rule.get('id', '?')}\n"
        f"      conditions: {conditions}\n"
        f"      actions:    {actions}"
    )


# The inbox-rule ops are async-only (Graph's messageRules endpoint), so the
# rules CLI commands wrap them in asyncio.run() rather than using GraphClient.
def cmd_rules_list(args: argparse.Namespace) -> None:
    import asyncio

    from ms_graph.graph_client import AsyncGraphClient

    async def _run():
        token = get_local_token()
        async with AsyncGraphClient(token) as client:
            return await mail.alist_inbox_rules(client)

    rules = asyncio.run(_run())
    if not rules:
        print("No inbox rules found.")
        return
    print(f"Inbox rules ({len(rules)}):\n")
    for rule in rules:
        print(_fmt_rule(rule))
        print()


def cmd_rules_get(args: argparse.Namespace) -> None:
    import asyncio

    from ms_graph.graph_client import AsyncGraphClient

    async def _run():
        token = get_local_token()
        async with AsyncGraphClient(token) as client:
            return await mail.aget_inbox_rule(client, args.rule_id)

    print(_fmt_rule(asyncio.run(_run())))


def cmd_rules_create(args: argparse.Namespace) -> None:
    import asyncio
    import json

    from ms_graph.graph_client import AsyncGraphClient

    with open(args.file, encoding="utf-8") as f:
        rule = json.load(f)

    async def _run():
        token = get_local_token()
        async with AsyncGraphClient(token) as client:
            return await mail.acreate_inbox_rule(client, rule)

    created = asyncio.run(_run())
    print(f"Rule {created.get('id', '?')} ({created.get('displayName', '?')}) created.")


def cmd_rules_update(args: argparse.Namespace) -> None:
    import asyncio
    import json

    from ms_graph.graph_client import AsyncGraphClient

    with open(args.file, encoding="utf-8") as f:
        changes = json.load(f)

    async def _run():
        token = get_local_token()
        async with AsyncGraphClient(token) as client:
            return await mail.aupdate_inbox_rule(client, args.rule_id, changes)

    updated = asyncio.run(_run())
    print(f"Rule {updated.get('id', args.rule_id)} ({updated.get('displayName', '?')}) updated.")


def cmd_rules_delete(args: argparse.Namespace) -> None:
    import asyncio

    from ms_graph.graph_client import AsyncGraphClient

    async def _run():
        token = get_local_token()
        async with AsyncGraphClient(token) as client:
            await mail.adelete_inbox_rule(client, args.rule_id)

    asyncio.run(_run())
    print(f"Rule {args.rule_id} deleted.")


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def cmd_calendar_list(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta
    from datetime import timezone as tz

    start = args.start
    end = args.end
    if not start:
        start = datetime.now(tz.utc).isoformat()
    if not end:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end = (start_dt + timedelta(days=7)).isoformat()

    token = get_local_token()
    with GraphClient(token) as client:
        events = calendar.list_calendar_events(
            client, start_datetime=start, end_datetime=end, top=args.top
        )

    if not events:
        print("No calendar events found in the specified date range.")
        return

    print(f"Calendar events ({len(events)}):\n")
    for i, event in enumerate(events, 1):
        subject = event.get("subject", "(no subject)")
        start_info = event.get("start", {})
        end_info = event.get("end", {})
        is_all_day = event.get("isAllDay", False)
        is_cancelled = event.get("isCancelled", False)
        location = event.get("location", {}).get("displayName", "")
        organizer = event.get("organizer", {}).get("emailAddress", {}).get("name", "")

        status = " [CANCELLED]" if is_cancelled else ""
        time_str = (
            "All day"
            if is_all_day
            else f"{start_info.get('dateTime', '?')} - {end_info.get('dateTime', '?')}"
        )

        print(f"[{i}] {subject}{status}")
        print(f"     Time: {time_str}")
        if organizer:
            print(f"     Organizer: {organizer}")
        if location:
            print(f"     Location: {location}")
        print(f"     ID: {event.get('id', '?')}")
        print()


def cmd_calendar_get(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        event = calendar.get_calendar_event(client, args.event_id)

    subject = event.get("subject", "(no subject)")
    start = event.get("start", {})
    end = event.get("end", {})
    is_all_day = event.get("isAllDay", False)
    location = event.get("location", {}).get("displayName", "")
    organizer = event.get("organizer", {}).get("emailAddress", {})
    online_url = event.get("onlineMeetingUrl", "")
    recurrence = event.get("recurrence")

    print(f"Subject:    {subject}")
    if is_all_day:
        print("Time:       All day")
    else:
        print(f"Start:      {start.get('dateTime', '?')} ({start.get('timeZone', '?')})")
        print(f"End:        {end.get('dateTime', '?')} ({end.get('timeZone', '?')})")
    if organizer:
        print(f"Organizer:  {organizer.get('name', '?')} <{organizer.get('address', '?')}>")
    if location:
        print(f"Location:   {location}")
    if online_url:
        print(f"Online:     {online_url}")
    if recurrence:
        pattern = recurrence.get("pattern", {})
        print(f"Recurrence: {pattern.get('type', '?')} (every {pattern.get('interval', 1)})")

    attendees = event.get("attendees", [])
    if attendees:
        print(f"\nAttendees ({len(attendees)}):")
        for att in attendees:
            email = att.get("emailAddress", {})
            status = att.get("status", {}).get("response", "none")
            print(f"  {email.get('name', '?')} <{email.get('address', '?')}> ({status})")

    body = event.get("body", {})
    content = body.get("content", "")
    if content:
        print(f"\n--- body ---\n{content[:3000]}")


def cmd_calendar_create(args: argparse.Namespace) -> None:
    attendee_list = (
        [a.strip() for a in args.attendees.split(",") if a.strip()] if args.attendees else None
    )

    token = get_local_token()
    with GraphClient(token) as client:
        event = calendar.create_calendar_event(
            client,
            subject=args.subject,
            start_datetime=args.start,
            start_timezone=args.timezone,
            end_datetime=args.end,
            end_timezone=args.timezone,
            body=args.body,
            attendees=attendee_list,
            location=args.location,
            is_online_meeting=args.online,
            is_all_day=args.all_day,
        )

    print(f"Event '{args.subject}' created successfully.")
    start = event.get("start", {})
    print(f"Time: {start.get('dateTime', '?')} ({start.get('timeZone', '?')})")
    if event.get("onlineMeetingUrl"):
        print(f"Meeting link: {event['onlineMeetingUrl']}")
    print(f"ID: {event.get('id', '?')}")


def cmd_calendar_availability(args: argparse.Namespace) -> None:
    email_list = [a.strip() for a in args.emails.split(",") if a.strip()]
    if not email_list:
        print("No email addresses provided.")
        return

    token = get_local_token()
    with GraphClient(token) as client:
        result = calendar.check_availability(
            client,
            schedules=email_list,
            start_datetime=args.start,
            start_timezone=args.timezone,
            end_datetime=args.end,
            end_timezone=args.timezone,
        )

    schedules = result.get("value", [])
    if not schedules:
        print("No availability information returned.")
        return

    print(f"Availability ({len(schedules)} schedule(s)):\n")
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

        print(f"  {email} — {summary}")
        for item in schedule_items[:10]:
            item_start = item.get("start", {}).get("dateTime", "?")
            item_end = item.get("end", {}).get("dateTime", "?")
            item_subject = item.get("subject", "(private)")
            item_status = item.get("status", "?")
            print(f"    {item_start} to {item_end}: {item_subject} ({item_status})")
        print()


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def cmd_teams_list(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        if args.team_id:
            channels = teams.list_channels(client, args.team_id)
            if not channels:
                print("No channels found.")
                return
            print(f"Channels in team {args.team_id} ({len(channels)}):\n")
            for ch in channels:
                print(f"  {ch.get('displayName', '?')}  [id: {ch.get('id', '?')}]")
        else:
            team_list = teams.list_joined_teams(client)
            if not team_list:
                print("No teams found.")
                return
            print(f"Joined teams ({len(team_list)}):\n")
            for t in team_list:
                print(f"  {t.get('displayName', '?')}  [id: {t.get('id', '?')}]")


def cmd_teams_chats(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        chat_list = teams.list_chats(client, chat_type=args.type, top=args.top)

    if not chat_list:
        print("No chats found.")
        return

    print(f"Chats ({len(chat_list)}):\n")
    for i, chat in enumerate(chat_list, 1):
        members = chat.get("members") or []
        member_names = [m.get("displayName", "?") for m in members if m.get("displayName")]
        label = chat.get("topic") or ", ".join(member_names[:3]) or "(unnamed)"
        print(f"[{i}] {label}  (type: {chat.get('chatType', '?')})  [id: {chat.get('id', '?')}]")


def cmd_teams_read(args: argparse.Namespace) -> None:
    if not args.chat_id and not (args.team_id and args.channel_id):
        print("Error: provide --chat-id, or both --team-id and --channel-id.", file=sys.stderr)
        sys.exit(1)

    token = get_local_token()
    with GraphClient(token) as client:
        if args.chat_id:
            messages = teams.list_chat_messages(client, args.chat_id, top=args.top)
            source = f"chat {args.chat_id}"
        else:
            messages = teams.list_channel_messages(
                client, args.team_id, args.channel_id, top=args.top
            )
            source = f"channel {args.channel_id}"

    if not messages:
        print(f"No messages in {source}.")
        return

    print(f"Messages in {source} ({len(messages)}):\n")
    for i, msg in enumerate(messages, 1):
        sender = teams.extract_message_sender(msg)
        content = teams.extract_message_text(msg)
        print(f"[{i}] {sender}  ({msg.get('createdDateTime', '?')})")
        print(f"     {content or '(empty)'}")
        print()


def cmd_teams_attachment(args: argparse.Namespace) -> None:
    if not args.chat_id and not (args.team_id and args.channel_id):
        print("Error: provide --chat-id, or both --team-id and --channel-id.", file=sys.stderr)
        sys.exit(1)

    kw = (
        {"chat_id": args.chat_id}
        if args.chat_id
        else {"team_id": args.team_id, "channel_id": args.channel_id}
    )

    token = get_local_token()
    with GraphClient(token) as client:
        msg = teams.get_message(client, args.message_id, **kw)
        entries = teams.parse_message_attachments(msg)
        entry = next((e for e in entries if e["id"] == args.attachment_id), None)
        if entry is None:
            available = ", ".join(f"{e['kind']}: {e['id']}" for e in entries if e["id"]) or "(none)"
            print(
                f"Error: no attachment '{args.attachment_id}' on this message. "
                f"Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)

        kind = entry["kind"]
        if kind == "card":
            print(
                f"Card ({entry['content_type']}): " f"{entry['card_text'] or '(no readable text)'}"
            )
            return
        if kind not in ("file", "image"):
            print(f"Nothing to download: this attachment is a {kind}.")
            return

        if kind == "file":
            if not entry["content_url"]:
                print("Error: this file attachment has no content URL.", file=sys.stderr)
                sys.exit(1)
            item, data = files.resolve_sharing_link_bytes(client, entry["content_url"])
            name = entry["name"] or item.get("name") or "attachment"
            ctype = (item.get("file") or {}).get("mimeType", "")
        else:
            data, ctype = teams.get_hosted_content(client, args.message_id, entry["id"], **kw)
            ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ".bin"
            name = f"image-{entry['id'][:12]}{ext}"

    out = args.out or _default_attachment_filename(name, "file")
    Path(out).write_bytes(data)
    print(f"Saved {len(data)} bytes to {out} ({ctype})")


def _read_attachments(paths: list[str]) -> list:
    """Read local files into attachments, or exit naming the one that is missing."""
    resolved = []
    for path in paths:
        source = Path(path)
        if not source.is_file():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        resolved.append(
            attachment_ops.ResolvedAttachment(
                name=source.name,
                data=source.read_bytes(),
                content_type=attachment_ops.guess_content_type(source.name),
            )
        )
    return resolved


def cmd_teams_send(args: argparse.Namespace) -> None:
    if not args.chat_id and not (args.team_id and args.channel_id):
        print("Error: provide --chat-id, or both --team-id and --channel-id.", file=sys.stderr)
        sys.exit(1)

    files = _read_attachments(args.attach)
    images = _read_attachments(args.image)
    target = "chat" if args.chat_id else "channel"
    notes = ""
    if files:
        notes += f" with {len(files)} file(s)"
    if images:
        notes += f" and {len(images)} image(s)" if files else f" with {len(images)} image(s)"

    token = get_local_token()
    with GraphClient(token) as client:
        if not files and not images:
            if args.chat_id:
                teams.send_chat_message(client, args.chat_id, args.message)
            else:
                teams.send_channel_message(client, args.team_id, args.channel_id, args.message)
            print(f"Message sent to {target}.")
            return
        try:
            teams.send_message_with_files(
                client,
                content=args.message,
                files=files,
                images=images,
                chat_id=args.chat_id,
                team_id="" if args.chat_id else args.team_id,
                channel_id="" if args.chat_id else args.channel_id,
                exclude_user_id=teams.decode_token_claims(token).get("oid", ""),
            )
        except teams.FilesScopeMissingError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    print(f"Message sent to {target}{notes}.")


def cmd_teams_activity(args: argparse.Namespace) -> None:
    # aget_teams_activity has no sync counterpart (it fans out across many channels
    # concurrently), so this is the one CLI command that needs asyncio.run().
    import asyncio

    from ms_graph.graph_client import AsyncGraphClient

    async def _run():
        token = get_local_token()
        async with AsyncGraphClient(token) as client:
            return await teams.aget_teams_activity(client, hours=args.hours)

    activity = asyncio.run(_run())
    if not activity:
        print(f"No Teams activity in the last {args.hours} hours.")
        return

    sources = {row["source_name"] for row in activity}
    print(
        f"Activity in the last {args.hours} hours: {len(activity)} messages across {len(sources)} sources\n"
    )
    for row in activity:
        print(
            f"  [{row['timestamp']}] {row['source_name']} — {row['sender']}: {row['preview'][:80]}"
        )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def cmd_files_sites(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        sites = files.list_sites(client, query=args.query, top=args.top)

    if not sites:
        print("No sites found.")
        return

    desc = f"matching '{args.query}'" if args.query else "followed"
    print(f"SharePoint sites {desc} ({len(sites)}):\n")
    for site in sites:
        name = site.get("displayName", site.get("name", "?"))
        print(f"  {name}  [id: {site.get('id', '?')}]")
        if site.get("webUrl"):
            print(f"    {site['webUrl']}")


def cmd_files_list(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        if args.query:
            results = files.search_files_unified(client, query=args.query, top=args.top)
            if not results:
                print(f"No files found matching '{args.query}'.")
                return
            print(f"Search results for '{args.query}' ({len(results)}):\n")
            for i, item in enumerate(results, 1):
                print(
                    f"[{i}] {item.get('name', '?')}  ({_fmt_size(item.get('size', 0))})  [id: {item.get('id', '?')}]"
                )
                if item.get("_searchSummary"):
                    print(f"     {item['_searchSummary']}")
                if item.get("webUrl"):
                    print(f"     {item['webUrl']}")
                print()
        else:
            items = files.list_drive_children(
                client, folder_path=args.path, site_id=args.site_id, top=args.top
            )
            if not items:
                print("No files found.")
                return
            loc = f"'{args.path}'" if args.path else "root"
            label = f"SharePoint {args.site_id}" if args.site_id else "OneDrive"
            print(f"Files in {label} / {loc} ({len(items)}):\n")
            for item in items:
                print(_fmt_drive_item(item))


def cmd_files_inspect(args: argparse.Namespace) -> None:
    token = get_local_token()
    sharing_url = ""
    item_id = args.item_id

    if files.is_sharing_url(item_id):
        sharing_url = item_id
        item_id = ""

    with GraphClient(token) as client:
        if sharing_url:
            if args.content:
                item, content = files.resolve_sharing_link_content(client, sharing_url)
                if content is None:
                    item, content = files.resolve_sharing_link_extracted_content(
                        client, sharing_url, item=item
                    )
            else:
                item = files.resolve_sharing_link(client, sharing_url)
                content = None
        else:
            if args.content:
                item, content = files.get_drive_item_content(client, item_id, site_id=args.site_id)
                if content is None:
                    item, content = files.get_drive_item_extracted_content(
                        client, item_id, site_id=args.site_id, item=item
                    )
            else:
                item = files.get_drive_item(client, item_id, site_id=args.site_id)
                content = None

    print(f"Name:     {item.get('name', '?')}")
    if "folder" in item:
        print(f"Type:     Folder ({item['folder'].get('childCount', '?')} items)")
    else:
        print(f"Type:     {item.get('file', {}).get('mimeType', '?')}")
        print(f"Size:     {_fmt_size(item.get('size', 0))}")
    print(
        f"Modified: {item.get('lastModifiedDateTime', '?')} by {item.get('lastModifiedBy', {}).get('user', {}).get('displayName', '?')}"
    )
    print(f"ID:       {item.get('id', '?')}")
    if item.get("webUrl"):
        print(f"URL:      {item['webUrl']}")

    if args.content:
        print()
        if content is not None:
            print("--- content ---")
            print(content)
        else:
            print("(binary or too large to display as text)")


def cmd_files_resolve(args: argparse.Namespace) -> None:
    """Resolve a sharing URL and display the underlying driveItem."""
    token = get_local_token()
    with GraphClient(token) as client:
        if args.content:
            item, content = files.resolve_sharing_link_content(client, args.url)
            if content is None:
                item, content = files.resolve_sharing_link_extracted_content(
                    client, args.url, item=item
                )
        else:
            item = files.resolve_sharing_link(client, args.url)
            content = None

    print(f"Name:     {item.get('name', '?')}")
    if "folder" in item:
        print(f"Type:     Folder ({item['folder'].get('childCount', '?')} items)")
    else:
        print(f"Type:     {item.get('file', {}).get('mimeType', '?')}")
        print(f"Size:     {_fmt_size(item.get('size', 0))}")
    print(
        f"Modified: {item.get('lastModifiedDateTime', '?')} by {item.get('lastModifiedBy', {}).get('user', {}).get('displayName', '?')}"
    )
    print(f"ID:       {item.get('id', '?')}")
    if item.get("webUrl"):
        print(f"URL:      {item['webUrl']}")

    if args.content:
        print()
        if content is not None:
            print("--- content ---")
            print(content)
        else:
            print("(binary or too large to display as text)")


def cmd_files_upload(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        item = files.upload_file(
            client,
            folder_path=args.folder,
            filename=args.filename,
            content=args.content,
            site_id=args.site_id,
        )

    print(f"Uploaded: {item.get('name', '?')}  ({_fmt_size(item.get('size', 0))})")
    print(f"ID:       {item.get('id', '?')}")
    if item.get("webUrl"):
        print(f"URL:      {item['webUrl']}")


def cmd_files_copy(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        status = files.copy_drive_item(
            client,
            item_id=args.item_id,
            new_name=args.new_name,
            destination_folder_id=args.dest_folder,
            site_id=args.site_id,
            destination_drive_id=args.dest_drive,
        )
    print(f"Copied as '{args.new_name}'.")
    print(f"New item ID: {status.get('resourceId', '?')}")


def cmd_files_rename(args: argparse.Namespace) -> None:
    token = get_local_token()
    with GraphClient(token) as client:
        item = files.rename_drive_item(
            client,
            item_id=args.item_id,
            new_name=args.new_name,
            site_id=args.site_id,
        )
    print(f"Renamed to '{item.get('name', args.new_name)}'.")
    print(f"ID: {item.get('id', '?')}")
    if item.get("webUrl"):
        print(f"URL: {item['webUrl']}")


# ---------------------------------------------------------------------------
# Power BI commands
# ---------------------------------------------------------------------------


def _resolve_workspace(workspace_id: str) -> str:
    """Translate 'me' to '' (My workspace uses root API paths, not /groups/{id})."""
    return "" if workspace_id.lower() == "me" else workspace_id


def cmd_pbi_workspaces(args: argparse.Namespace) -> None:
    token = get_local_powerbi_token()
    with PowerBIClient(token) as client:
        workspaces = pbi_ops.list_workspaces(client)

    print(f"Workspaces ({len(workspaces) + 1}):\n")
    print("  My workspace  [id: me]")
    for ws in workspaces:
        capacity = " [Premium]" if ws.get("isOnDedicatedCapacity") else ""
        print(f"  {ws.get('name', '?')}{capacity}  [id: {ws.get('id', '?')}]")


def cmd_pbi_content(args: argparse.Namespace) -> None:
    token = get_local_powerbi_token()
    workspace_id = _resolve_workspace(args.workspace_id)
    content_type = args.type.lower() if args.type else "all"

    with PowerBIClient(token) as client:
        if content_type in ("datasets", "all"):
            datasets = pbi_ops.list_datasets(client, workspace_id)
        else:
            datasets = []
        if content_type in ("reports", "all"):
            reports = pbi_ops.list_reports(client, workspace_id)
        else:
            reports = []
        if content_type in ("dashboards", "all"):
            dashboards = pbi_ops.list_dashboards(client, workspace_id)
        else:
            dashboards = []

    if datasets:
        print(f"Datasets ({len(datasets)}):")
        for ds in datasets:
            refreshable = " [refreshable]" if ds.get("isRefreshable") else ""
            print(f"  {ds.get('name', '?')}{refreshable}  [id: {ds.get('id', '?')}]")
        print()
    if reports:
        print(f"Reports ({len(reports)}):")
        for r in reports:
            print(
                f"  {r.get('name', '?')}  [id: {r.get('id', '?')}]  dataset: {r.get('datasetId', '?')}"
            )
        print()
    if dashboards:
        print(f"Dashboards ({len(dashboards)}):")
        for d in dashboards:
            print(f"  {d.get('displayName', '?')}  [id: {d.get('id', '?')}]")
        print()
    if not datasets and not reports and not dashboards:
        print("No content found.")


def cmd_pbi_query(args: argparse.Namespace) -> None:
    token = get_local_powerbi_token()
    ws = _resolve_workspace(args.workspace_id)
    with PowerBIClient(token) as client:
        result = pbi_ops.execute_dax_query(client, ws, args.dataset_id, args.dax_query)

    csv_output = pbi_ops._format_dax_results(result)
    print(csv_output)


def cmd_pbi_refresh(args: argparse.Namespace) -> None:
    token = get_local_powerbi_token()
    ws = _resolve_workspace(args.workspace_id)
    if args.history:
        with PowerBIClient(token) as client:
            history = pbi_ops.get_refresh_history(client, ws, args.dataset_id, top=args.top)
        if not history:
            print("No refresh history found.")
            return
        print(f"Refresh history ({len(history)}):\n")
        for entry in history:
            start = entry.get("startTime", "?")
            end = entry.get("endTime", "?")
            status = entry.get("status", "?")
            rtype = entry.get("refreshType", "?")
            print(f"  [{status}] {rtype}  {start} → {end}")
    else:
        with PowerBIClient(token) as client:
            pbi_ops.trigger_refresh(client, ws, args.dataset_id)
        print(f"Refresh triggered for dataset {args.dataset_id}.")
        print("Use --history to check refresh status.")


def cmd_pbi_export(args: argparse.Namespace) -> None:
    token = get_local_powerbi_token()
    ws = _resolve_workspace(args.workspace_id)
    pages = [p.strip() for p in args.pages.split(",")] if args.pages else None
    export_format = args.format.upper()

    print(f"Starting {export_format} export for report {args.report_id}...")
    with PowerBIClient(token) as client:
        export_id = pbi_ops.start_export(client, ws, args.report_id, export_format, pages=pages)
        print(f"Export started (ID: {export_id}). Waiting for completion...")
        status = pbi_ops.poll_export(client, ws, args.report_id, export_id)
        print("Export succeeded. Downloading...")
        data = pbi_ops.download_export(client, ws, args.report_id, export_id)

    ext = status.get("resourceFileExtension", f".{export_format.lower()}")
    if args.out:
        out_path = args.out
    else:
        out_path = f"report-{args.report_id}{ext}"

    with open(out_path, "wb") as f:
        f.write(data)

    print(f"Saved {len(data):,} bytes to: {out_path}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Microsoft Graph CLI — mirrors the 23 consolidated MCP tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # whoami
    p = sub.add_parser("whoami", help="Show authenticated user profile")
    p.set_defaults(func=cmd_whoami)

    # powerbi-whoami
    p = sub.add_parser(
        "powerbi-whoami", help="Verify Power BI token (triggers browser auth if needed)"
    )
    p.set_defaults(func=cmd_powerbi_whoami)

    # email
    p_email = sub.add_parser("email", help="Email operations")
    email_sub = p_email.add_subparsers(dest="email_command", required=True)

    p = email_sub.add_parser("list", help="List or search emails")
    p.add_argument("--folder", default="inbox", help="Folder to list (ignored when --query is set)")
    p.add_argument("--query", default="", help="Search query (empty = list folder)")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_email_list)

    p = email_sub.add_parser("read", help="Read a single email by ID")
    p.add_argument("message_id")
    p.set_defaults(func=cmd_email_read)

    p = email_sub.add_parser("attachment", help="Download an email attachment to a file")
    p.add_argument("message_id")
    p.add_argument("attachment_id")
    p.add_argument(
        "--out",
        default=None,
        help="Output path (default: the attachment's own name in the current directory)",
    )
    p.set_defaults(func=cmd_email_attachment)

    p = email_sub.add_parser("send", help="Send an email")
    p.add_argument("to", help="Recipient email address")
    p.add_argument("subject")
    p.add_argument("body")
    p.add_argument("--from", dest="from_address", default=None, help="Sender alias address")
    p.add_argument("--cc", default="", help="CC recipients (comma-separated)")
    p.add_argument(
        "--attach", action="append", default=[], metavar="PATH", help="File to attach (repeatable)"
    )
    p.set_defaults(func=cmd_email_send)

    # rules
    p_rules = sub.add_parser("rules", help="Inbox rule (messageRule) operations")
    rules_sub = p_rules.add_subparsers(dest="rules_command", required=True)

    p = rules_sub.add_parser("list", help="List inbox rules")
    p.set_defaults(func=cmd_rules_list)

    p = rules_sub.add_parser("get", help="Get a single inbox rule by ID")
    p.add_argument("rule_id")
    p.set_defaults(func=cmd_rules_get)

    p = rules_sub.add_parser("create", help="Create an inbox rule from a JSON file")
    p.add_argument("--file", required=True, help="Path to a JSON file with the rule definition")
    p.set_defaults(func=cmd_rules_create)

    p = rules_sub.add_parser("update", help="Update an inbox rule from a JSON file")
    p.add_argument("rule_id")
    p.add_argument("--file", required=True, help="Path to a JSON file with the changed fields")
    p.set_defaults(func=cmd_rules_update)

    p = rules_sub.add_parser("delete", help="Delete an inbox rule by ID")
    p.add_argument("rule_id")
    p.set_defaults(func=cmd_rules_delete)

    # calendar
    p_cal = sub.add_parser("calendar", help="Calendar operations")
    cal_sub = p_cal.add_subparsers(dest="calendar_command", required=True)

    p = cal_sub.add_parser("list", help="List calendar events in a date range")
    p.add_argument("--start", default="", help="Start datetime ISO 8601 (default: now)")
    p.add_argument("--end", default="", help="End datetime ISO 8601 (default: +7 days)")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_calendar_list)

    p = cal_sub.add_parser("get", help="Get full details of a calendar event")
    p.add_argument("event_id", help="Event ID (from calendar list)")
    p.set_defaults(func=cmd_calendar_get)

    p = cal_sub.add_parser("create", help="Create a new calendar event")
    p.add_argument("subject", help="Event title")
    p.add_argument("start", help="Start datetime ISO 8601 (e.g. 2026-05-08T10:00:00)")
    p.add_argument("end", help="End datetime ISO 8601 (e.g. 2026-05-08T11:00:00)")
    p.add_argument("--timezone", default="UTC", help="IANA timezone (default: UTC)")
    p.add_argument("--attendees", default="", help="Comma-separated email addresses")
    p.add_argument("--location", default="", help="Event location")
    p.add_argument("--body", default="", help="Event description")
    p.add_argument("--online", action="store_true", help="Create Teams meeting link")
    p.add_argument("--all-day", dest="all_day", action="store_true", help="All-day event")
    p.set_defaults(func=cmd_calendar_create)

    p = cal_sub.add_parser("availability", help="Check free/busy for people")
    p.add_argument("emails", help="Comma-separated email addresses")
    p.add_argument("start", help="Start datetime ISO 8601")
    p.add_argument("end", help="End datetime ISO 8601")
    p.add_argument("--timezone", default="UTC", help="IANA timezone (default: UTC)")
    p.set_defaults(func=cmd_calendar_availability)

    # teams
    p_teams = sub.add_parser("teams", help="Teams operations")
    teams_sub = p_teams.add_subparsers(dest="teams_command", required=True)

    p = teams_sub.add_parser("list", help="List joined teams, or channels within a team")
    p.add_argument("--team-id", dest="team_id", default="", help="Team ID to list channels for")
    p.set_defaults(func=cmd_teams_list)

    p = teams_sub.add_parser("chats", help="List chats (1:1, group, meeting)")
    p.add_argument("--type", default="", help="Filter: oneOnOne, group, or meeting")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_teams_chats)

    p = teams_sub.add_parser("read", help="Read messages from a channel or chat")
    p.add_argument("--team-id", dest="team_id", default="")
    p.add_argument("--channel-id", dest="channel_id", default="")
    p.add_argument("--chat-id", dest="chat_id", default="")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_teams_read)

    p = teams_sub.add_parser(
        "attachment", help="Download a file or inline image from a Teams message"
    )
    p.add_argument("message_id")
    p.add_argument("attachment_id")
    p.add_argument("--team-id", dest="team_id", default="")
    p.add_argument("--channel-id", dest="channel_id", default="")
    p.add_argument("--chat-id", dest="chat_id", default="")
    p.add_argument("--out", default="", help="Output path (default: the attachment's own name)")
    p.set_defaults(func=cmd_teams_attachment)

    p = teams_sub.add_parser("send", help="Send a message to a channel or chat")
    p.add_argument("message")
    p.add_argument("--team-id", dest="team_id", default="")
    p.add_argument("--channel-id", dest="channel_id", default="")
    p.add_argument("--chat-id", dest="chat_id", default="")
    p.add_argument(
        "--attach", action="append", default=[], metavar="PATH", help="File to send (repeatable)"
    )
    p.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="Image shown inline in the message (repeatable, image/* under 4 MB)",
    )
    p.set_defaults(func=cmd_teams_send)

    p = teams_sub.add_parser("activity", help="Recent Teams activity digest")
    p.add_argument("--hours", type=int, default=24)
    p.set_defaults(func=cmd_teams_activity)

    # files
    p_files = sub.add_parser("files", help="File / OneDrive / SharePoint operations")
    files_sub = p_files.add_subparsers(dest="files_command", required=True)

    p = files_sub.add_parser("sites", help="List or search SharePoint sites")
    p.add_argument("--query", default="", help="Search query (empty = followed sites)")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_files_sites)

    p = files_sub.add_parser("list", help="List or search files in OneDrive / SharePoint")
    p.add_argument("--path", default="", help="Folder path (e.g. Documents/Reports)")
    p.add_argument(
        "--site-id", dest="site_id", default="", help="SharePoint site ID (empty = OneDrive)"
    )
    p.add_argument("--query", default="", help="Search query across all drives")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_files_list)

    p = files_sub.add_parser("inspect", help="Get file metadata (and optionally content)")
    p.add_argument("item_id", help="Drive item ID or sharing URL")
    p.add_argument("--site-id", dest="site_id", default="")
    p.add_argument("--content", action="store_true", help="Also download and print text content")
    p.set_defaults(func=cmd_files_inspect)

    p = files_sub.add_parser("resolve", help="Resolve a sharing URL to a driveItem")
    p.add_argument("url", help="SharePoint/OneDrive sharing URL")
    p.add_argument("--content", action="store_true", help="Also download and print text content")
    p.set_defaults(func=cmd_files_resolve)

    p = files_sub.add_parser("upload", help="Create or overwrite a text file")
    p.add_argument("filename", help="File name with extension (e.g. report.md)")
    p.add_argument("content", help="Text content to write")
    p.add_argument("--folder", default="", help="Destination folder path")
    p.add_argument("--site-id", dest="site_id", default="", help="SharePoint site ID")
    p.set_defaults(func=cmd_files_upload)

    p = files_sub.add_parser("copy", help="Server-side copy of a file")
    p.add_argument("item_id", help="Source file item ID")
    p.add_argument("new_name", help="Name for the copy (with extension)")
    p.add_argument(
        "--dest-folder", dest="dest_folder", default="", help="Destination folder item ID"
    )
    p.add_argument("--site-id", dest="site_id", default="", help="SharePoint site ID of source")
    p.add_argument("--dest-drive", dest="dest_drive", default="", help="Destination drive ID")
    p.set_defaults(func=cmd_files_copy)

    p = files_sub.add_parser("rename", help="Rename a file or folder")
    p.add_argument("item_id")
    p.add_argument("new_name", help="New name (with extension)")
    p.add_argument("--site-id", dest="site_id", default="")
    p.set_defaults(func=cmd_files_rename)

    # powerbi
    p_pbi = sub.add_parser("powerbi", help="Power BI operations")
    pbi_sub = p_pbi.add_subparsers(dest="pbi_command", required=True)

    p = pbi_sub.add_parser("workspaces", help="List Power BI workspaces")
    p.set_defaults(func=cmd_pbi_workspaces)

    p = pbi_sub.add_parser("content", help="List datasets, reports, and dashboards in a workspace")
    p.add_argument("workspace_id")
    p.add_argument("--type", default="all", help="datasets, reports, dashboards, or all (default)")
    p.set_defaults(func=cmd_pbi_content)

    p = pbi_sub.add_parser("query", help="Execute a DAX query against a dataset")
    p.add_argument("workspace_id")
    p.add_argument("dataset_id")
    p.add_argument("dax_query", help="DAX query string (e.g. \"EVALUATE TOPN(10, 'Sales')\")")
    p.set_defaults(func=cmd_pbi_query)

    p = pbi_sub.add_parser("refresh", help="Trigger or inspect a dataset refresh")
    p.add_argument("workspace_id")
    p.add_argument("dataset_id")
    p.add_argument(
        "--history", action="store_true", help="Show refresh history instead of triggering"
    )
    p.add_argument("--top", type=int, default=5, help="Number of history entries (default: 5)")
    p.set_defaults(func=cmd_pbi_refresh)

    p = pbi_sub.add_parser("export", help="Export a report to PDF, PNG, or PPTX")
    p.add_argument("workspace_id")
    p.add_argument("report_id")
    p.add_argument("--format", default="PDF", help="Export format: PDF (default), PNG, or PPTX")
    p.add_argument(
        "--pages", default="", help="Comma-separated page names to export (default: all)"
    )
    p.add_argument("--out", default="", help="Output file path (default: report-<id>.<ext>)")
    p.set_defaults(func=cmd_pbi_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
