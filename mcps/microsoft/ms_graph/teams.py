"""
Teams operations using the Microsoft Graph API.

All functions accept a GraphClient or AsyncGraphClient and return parsed dicts.
"""

import asyncio
import base64
import html as html_mod
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from .graph_client import AsyncGraphClient, GraphClient, GraphError
from .mail import _detect_body_type
from .pagination import apaginate, apaginate_until_date

logger = logging.getLogger(__name__)


def _safe_id(value: str) -> str:
    """URL-encode an ID to prevent path traversal in Graph API URLs."""
    return quote(value, safe="")


class TeamsNotAvailableError(Exception):
    """Raised when Teams operations fail because the account lacks a Teams license."""

    def __init__(self) -> None:
        super().__init__(
            "Microsoft Teams is not available for this account. "
            "Teams requires a Microsoft 365 business or developer license."
        )


def _check_teams_access(e: GraphError) -> None:
    """Raise TeamsNotAvailableError for 403 responses on Teams endpoints."""
    if e.status_code == 403:
        raise TeamsNotAvailableError() from e
    raise e


def decode_token_claims(token: str) -> dict[str, str]:
    """Extract oid and tid from a Microsoft Graph access token (JWT).

    Decodes the payload without signature verification — the token is already
    authenticated by the OAuth flow.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {"oid": "", "tid": ""}
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(payload.decode("utf-8"))
        return {"oid": claims.get("oid", ""), "tid": claims.get("tid", "")}
    except Exception:
        return {"oid": "", "tid": ""}


_VALID_CONTENT_TYPES = frozenset({"html", "text", "auto"})


def _prepare_teams_body(content: str, content_type: str = "auto") -> dict[str, str]:
    """Prepare a Teams message body dict with appropriate contentType.

    In "auto" mode (default), HTML content is detected and newlines are
    converted to <br>; plain text also has entities escaped before conversion.
    """
    content_type = content_type.lower()
    if content_type not in _VALID_CONTENT_TYPES:
        raise ValueError(f"content_type must be 'auto', 'html', or 'text'; got {content_type!r}")

    if content_type == "text":
        return {"contentType": "text", "content": content}
    if content_type == "html":
        return {"contentType": "html", "content": content}

    # Normalize CRLF/CR to LF before converting newlines
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    # Auto: detect HTML or convert plain text
    if _detect_body_type(content) == "HTML":
        return {"contentType": "html", "content": content.replace("\n", "<br>")}
    escaped = html_mod.escape(content, quote=False)
    return {"contentType": "html", "content": escaped.replace("\n", "<br>")}


# ---------------------------------------------------------------------------
# Message text / sender extraction helpers
# ---------------------------------------------------------------------------


def _extract_adaptive_card_text(card_json: str) -> str:
    """Extract readable text from an adaptive card JSON string."""
    try:
        card = json.loads(card_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    texts: list[str] = []

    def _walk(items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text and isinstance(text, str):
                texts.append(text)
            # Recurse into containers, columns, column sets, etc.
            for key in ("body", "items", "columns", "actions"):
                children = item.get(key)
                if isinstance(children, list):
                    _walk(children)

    body = card.get("body")
    if isinstance(body, list):
        _walk(body)
    return " | ".join(texts)


def extract_message_text(msg: dict[str, Any], max_length: int = -1) -> str:
    """Extract readable text from a Teams message.

    Handles plain text, HTML (strips tags), and adaptive card attachments.
    """
    body = msg.get("body") or {}
    content = body.get("content", "")

    # Strip HTML tags if needed
    if body.get("contentType") == "html" and content:
        content = re.sub(r"<[^>]+>", "", content).strip()

    # If body is empty, try adaptive card attachments
    if not content.strip():
        for att in msg.get("attachments") or []:
            if att.get("contentType") == "application/vnd.microsoft.card.adaptive":
                card_text = _extract_adaptive_card_text(att.get("content", ""))
                if card_text:
                    content = f"[Card] {card_text}"
                    break

    if not content.strip():
        return ""

    if max_length > 0 and len(content) > max_length:
        content = content[:max_length] + "..."
    return content


def extract_message_sender(msg: dict[str, Any]) -> str:
    """Extract the display name of the message sender."""
    sender = msg.get("from") or {}
    user = sender.get("user") or {}
    app = sender.get("application") or {}
    return user.get("displayName") or app.get("displayName") or "(system)"


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------


def list_joined_teams(client: GraphClient) -> list[dict[str, Any]]:
    """List teams the current user has joined."""
    try:
        data = client.get("/me/joinedTeams")
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


def list_channels(client: GraphClient, team_id: str) -> list[dict[str, Any]]:
    """List channels in a team."""
    try:
        data = client.get(f"/teams/{_safe_id(team_id)}/channels")
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


def send_channel_message(
    client: GraphClient,
    team_id: str,
    channel_id: str,
    content: str,
    content_type: str = "auto",
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a Teams channel."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    try:
        result = client.post(
            f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages",
            json_data=payload,
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


def list_channel_messages(
    client: GraphClient,
    team_id: str,
    channel_id: str,
    top: int = 20,
) -> list[dict[str, Any]]:
    """List recent messages in a Teams channel."""
    try:
        data = client.get(
            f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages",
            params={"$top": top},
        )
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


def list_chats(
    client: GraphClient,
    chat_type: str = "",
    top: int = 50,
) -> list[dict[str, Any]]:
    """List the user's recent chats (1:1, group, meeting).

    Note: top is capped at 50 (the MS Graph per-page max).
    Use alist_chats for async pagination beyond 50 results.
    """
    top = min(top, 50)
    params: dict[str, Any] = {
        "$top": top,
        "$expand": "lastMessagePreview,members",
        "$orderby": "lastMessagePreview/createdDateTime desc",
    }
    if chat_type:
        escaped = chat_type.replace("'", "''")
        params["$filter"] = f"chatType eq '{escaped}'"
    try:
        data = client.get("/me/chats", params=params)
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


def list_chat_messages(
    client: GraphClient,
    chat_id: str,
    top: int = 20,
) -> list[dict[str, Any]]:
    """List recent messages in a chat."""
    try:
        data = client.get(
            f"/chats/{_safe_id(chat_id)}/messages",
            params={"$top": top},
        )
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


def send_chat_message(
    client: GraphClient,
    chat_id: str,
    content: str,
    content_type: str = "auto",
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a chat."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    try:
        result = client.post(
            f"/chats/{_safe_id(chat_id)}/messages",
            json_data=payload,
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------


async def alist_joined_teams(client: AsyncGraphClient) -> list[dict[str, Any]]:
    """List teams the current user has joined (async)."""
    try:
        data = await client.get("/me/joinedTeams")
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


async def alist_channels(client: AsyncGraphClient, team_id: str) -> list[dict[str, Any]]:
    """List channels in a team (async)."""
    try:
        data = await client.get(f"/teams/{_safe_id(team_id)}/channels")
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


async def asend_channel_message(
    client: AsyncGraphClient,
    team_id: str,
    channel_id: str,
    content: str,
    content_type: str = "auto",
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a Teams channel (async)."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    try:
        result = await client.post(
            f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages",
            json_data=payload,
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


async def alist_channel_messages(
    client: AsyncGraphClient,
    team_id: str,
    channel_id: str,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """List messages in a Teams channel (async), paginating back to `since` date.

    Args:
        since: ISO 8601 date string. Fetches all messages from this date forward.
               Defaults to 7 days ago if not provided.
    """
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict[str, Any] = {}
    try:
        return await apaginate_until_date(
            client,
            f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages",
            params,
            since=since,
            page_size=50,
        )
    except GraphError as e:
        _check_teams_access(e)
    return []


_CHATS_PAGE_SIZE = 50


async def alist_chats(
    client: AsyncGraphClient,
    chat_type: str = "",
    top: int = 50,
) -> list[dict[str, Any]]:
    """List the user's recent chats (async), paginating internally if top > 50."""
    params: dict[str, Any] = {
        "$expand": "lastMessagePreview,members",
        "$orderby": "lastMessagePreview/createdDateTime desc",
    }
    if chat_type:
        escaped = chat_type.replace("'", "''")
        params["$filter"] = f"chatType eq '{escaped}'"
    try:
        return await apaginate(
            client,
            "/me/chats",
            params,
            max_results=top,
            page_size=_CHATS_PAGE_SIZE,
            max_pages=40,
        )
    except GraphError as e:
        _check_teams_access(e)
    return []


async def alist_chat_messages(
    client: AsyncGraphClient,
    chat_id: str,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """List messages in a chat (async), paginating back to `since` date.

    Args:
        since: ISO 8601 date string. Fetches all messages from this date forward.
               Defaults to 7 days ago if not provided.
    """
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict[str, Any] = {"$orderby": "createdDateTime desc"}
    try:
        return await apaginate_until_date(
            client,
            f"/chats/{_safe_id(chat_id)}/messages",
            params,
            since=since,
            page_size=50,
        )
    except GraphError as e:
        _check_teams_access(e)
    return []


async def amark_chat_read(
    client: AsyncGraphClient,
    chat_id: str,
    user_id: str,
    tenant_id: str,
) -> None:
    """Mark a chat as read for the specified user.

    Calls POST /chats/{chatId}/markChatReadForUser.
    Requires Chat.ReadWrite scope.
    """
    try:
        await client.post(
            f"/chats/{_safe_id(chat_id)}/markChatReadForUser",
            json_data={"user": {"id": user_id, "tenantId": tenant_id}},
        )
    except GraphError as e:
        _check_teams_access(e)


async def asend_chat_message(
    client: AsyncGraphClient,
    chat_id: str,
    content: str,
    content_type: str = "auto",
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a chat (async)."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    try:
        result = await client.post(
            f"/chats/{_safe_id(chat_id)}/messages",
            json_data=payload,
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


# ---------------------------------------------------------------------------
# Activity aggregator (async-only)
# ---------------------------------------------------------------------------


async def aget_teams_activity(
    client: AsyncGraphClient,
    hours: int = 24,
    max_channels: int = 50,
) -> list[dict[str, Any]]:
    """Aggregate recent Teams activity across channels and chats.

    Returns a list of dicts with keys: source, source_name, sender, timestamp, preview.
    Sorted by timestamp descending.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    sem = asyncio.Semaphore(20)

    async def _safe(coro):
        async with sem:
            return await coro

    # Step 1: Fetch teams and chats in parallel
    teams_result, chats_result = await asyncio.gather(
        _safe(alist_joined_teams(client)),
        _safe(alist_chats(client, top=50)),
        return_exceptions=True,
    )

    # Re-raise TeamsNotAvailableError so callers can distinguish "no activity" from "Teams not licensed"
    if isinstance(teams_result, TeamsNotAvailableError):
        raise teams_result
    if isinstance(chats_result, TeamsNotAvailableError):
        raise chats_result

    teams_list = teams_result if isinstance(teams_result, list) else []
    chats_list = chats_result if isinstance(chats_result, list) else []

    if isinstance(teams_result, Exception):
        logger.warning("Failed to fetch teams for activity: %s", teams_result)
    if isinstance(chats_result, Exception):
        logger.warning("Failed to fetch chats for activity: %s", chats_result)

    # Step 2: Fetch channels for each team in parallel
    channel_results = await asyncio.gather(
        *[_safe(alist_channels(client, t["id"])) for t in teams_list],
        return_exceptions=True,
    )

    # Build (team_name, team_id, channel) tuples
    channel_pairs: list[tuple[str, str, dict]] = []
    for team, ch_result in zip(teams_list, channel_results, strict=False):
        if isinstance(ch_result, Exception):
            logger.warning(
                "Failed to fetch channels for team %s: %s", team.get("displayName"), ch_result
            )
            continue
        for ch in ch_result:
            channel_pairs.append((team.get("displayName", "?"), team["id"], ch))

    # Cap channels
    channel_pairs = channel_pairs[:max_channels]

    # Step 3: Fetch latest message from each channel in parallel
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    msg_results = await asyncio.gather(
        *[
            _safe(alist_channel_messages(client, team_id, ch["id"], since=cutoff_str))
            for _, team_id, ch in channel_pairs
        ],
        return_exceptions=True,
    )

    # Build activity list
    activity: list[dict[str, Any]] = []

    def _parse_ts(ts_str: str) -> datetime | None:
        """Parse an ISO timestamp from Graph API (handles both Z and +00:00)."""
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    # Process channel messages
    for (team_name, _, ch), msgs in zip(channel_pairs, msg_results, strict=False):
        if isinstance(msgs, Exception) or not msgs:
            continue
        msg = msgs[0]
        ts = msg.get("createdDateTime", "")
        parsed = _parse_ts(ts)
        if parsed and parsed >= cutoff:
            activity.append(
                {
                    "source": "channel",
                    "source_name": f"{team_name} > {ch.get('displayName', '?')}",
                    "sender": extract_message_sender(msg),
                    "timestamp": ts,
                    "preview": extract_message_text(msg, max_length=200),
                }
            )

    # Process chats from lastMessagePreview (no extra API calls)
    for chat in chats_list:
        preview = chat.get("lastMessagePreview") or {}
        ts = preview.get("createdDateTime", "")
        parsed = _parse_ts(ts)
        if not parsed or parsed < cutoff:
            continue

        chat_type = chat.get("chatType", "?")
        topic = chat.get("topic")
        members = chat.get("members") or []
        member_names = [m.get("displayName", "?") for m in members if m.get("displayName")]

        if topic:
            source_name = f"{chat_type}: {topic}"
        elif member_names:
            source_name = f"{chat_type}: {', '.join(member_names[:4])}"
        else:
            source_name = chat_type

        preview_sender = (preview.get("from") or {}).get("user") or {}
        sender = preview_sender.get("displayName") or "(unknown)"
        preview_body = (preview.get("body") or {}).get("content", "")

        activity.append(
            {
                "source": "chat",
                "source_name": source_name,
                "sender": sender,
                "timestamp": ts,
                "preview": preview_body[:200],
            }
        )

    # Sort by timestamp descending
    activity.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return activity


# ---------------------------------------------------------------------------
# Desktop JSON operations (paged chat reads)
#
# These return RAW Graph response dicts and deliberately do NOT translate 403
# into TeamsNotAvailableError — the desktop client distinguishes "not granted"
# from "transient" itself. Query strings are built by hand because Graph's
# OData parser rejects ``+`` as a space in $filter/$orderby.
# ---------------------------------------------------------------------------

# ONE property drives both $orderby and $filter on /chats/{id}/messages.
# Graph SILENTLY ignores a $filter whose property differs from the $orderby
# property — you get an unfiltered page and no error — so the pairing must be
# structurally impossible to break.
CHAT_MESSAGE_SORT_PROP = "lastModifiedDateTime"

# lastUpdatedDateTime is NOT sortable on /me/chats; the preview timestamp is.
_CHATS_ORDERBY = "lastMessagePreview/createdDateTime desc"


def _chats_page_path(top: int) -> str:
    """Build the fresh-start /me/chats page URL."""
    return f"/me/chats?$top={top}&$expand=lastMessagePreview&$orderby={quote(_CHATS_ORDERBY)}"


def _chat_messages_page_path(chat_id: str, since: str) -> str:
    """Build the fresh-start chat messages page URL."""
    path = (
        f"/chats/{_safe_id(chat_id)}/messages"
        f"?$top=50&$orderby={quote(f'{CHAT_MESSAGE_SORT_PROP} desc')}"
    )
    if since:
        path += f"&$filter={quote(f'{CHAT_MESSAGE_SORT_PROP} gt {since}')}"
    return path


def chats_page(client: GraphClient, cursor: str = "", top: int = 50) -> dict[str, Any]:
    """Fetch ONE page of the user's chats. A non-empty cursor is fetched verbatim."""
    if cursor:
        return client.get(cursor)
    return client.get(_chats_page_path(top))


# /chats/{id}/members rejects $top outright ("Query option 'Top' is not
# allowed"), so Graph alone picks the page size and we cannot ask for the whole
# roster in one call. Teams chats hold up to 250 participants; 20 pages covers
# that even if Graph pages in small chunks.
_MAX_MEMBER_PAGES = 20


def _members_path(chat_id: str) -> str:
    """Build the members URL. No query options — see _MAX_MEMBER_PAGES."""
    return f"/chats/{_safe_id(chat_id)}/members"


def _merge_member_pages(last_page: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    """Graph-shaped response carrying every member collected.

    Keeps the final page's other keys, so an @odata.nextLink survives when the
    page cap cut the walk short — the caller can still see it was truncated.
    """
    return {**last_page, "value": members}


def chat_members(client: GraphClient, chat_id: str) -> dict[str, Any]:
    """Fetch a chat's full member list, following @odata.nextLink."""
    data = client.get(_members_path(chat_id))
    members = list(data.get("value", []))

    pages = 1
    while pages < _MAX_MEMBER_PAGES:
        next_link = data.get("@odata.nextLink")
        if not next_link:
            break
        data = client.get(next_link)
        members.extend(data.get("value", []))
        pages += 1

    return _merge_member_pages(data, members)


def chat_messages_page(
    client: GraphClient, chat_id: str, since: str = "", cursor: str = ""
) -> dict[str, Any]:
    """Fetch ONE page of a chat's messages. A non-empty cursor is fetched verbatim."""
    if cursor:
        return client.get(cursor)
    return client.get(_chat_messages_page_path(chat_id, since))


async def achats_page(client: AsyncGraphClient, cursor: str = "", top: int = 50) -> dict[str, Any]:
    """Fetch ONE page of the user's chats (async)."""
    if cursor:
        return await client.get(cursor)
    return await client.get(_chats_page_path(top))


async def achat_members(client: AsyncGraphClient, chat_id: str) -> dict[str, Any]:
    """Fetch a chat's full member list (async). No ``$top`` — see chat_members."""
    data = await client.get(_members_path(chat_id))
    members = list(data.get("value", []))

    pages = 1
    while pages < _MAX_MEMBER_PAGES:
        next_link = data.get("@odata.nextLink")
        if not next_link:
            break
        data = await client.get(next_link)
        members.extend(data.get("value", []))
        pages += 1

    return _merge_member_pages(data, members)


async def achat_messages_page(
    client: AsyncGraphClient, chat_id: str, since: str = "", cursor: str = ""
) -> dict[str, Any]:
    """Fetch ONE page of a chat's messages (async)."""
    if cursor:
        return await client.get(cursor)
    return await client.get(_chat_messages_page_path(chat_id, since))
