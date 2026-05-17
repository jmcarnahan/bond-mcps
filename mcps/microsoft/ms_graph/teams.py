"""
Teams operations using the Microsoft Graph API.

All functions accept a GraphClient or AsyncGraphClient and return parsed dicts.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from urllib.parse import quote

from .graph_client import GraphClient, AsyncGraphClient, GraphError

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


def extract_message_text(msg: Dict[str, Any], max_length: int = 500) -> str:
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

    if len(content) > max_length:
        content = content[:max_length] + "..."
    return content


def extract_message_sender(msg: Dict[str, Any]) -> str:
    """Extract the display name of the message sender."""
    sender = msg.get("from") or {}
    user = sender.get("user") or {}
    app = sender.get("application") or {}
    return user.get("displayName") or app.get("displayName") or "(system)"


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------

def list_joined_teams(client: GraphClient) -> List[Dict[str, Any]]:
    """List teams the current user has joined."""
    try:
        data = client.get("/me/joinedTeams")
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


def list_channels(client: GraphClient, team_id: str) -> List[Dict[str, Any]]:
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
) -> Dict[str, Any]:
    """Send a message to a Teams channel."""
    try:
        result = client.post(
            f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages",
            json_data={"body": {"content": content}},
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


def list_channel_messages(
    client: GraphClient,
    team_id: str,
    channel_id: str,
    top: int = 20,
) -> List[Dict[str, Any]]:
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
    top: int = 20,
) -> List[Dict[str, Any]]:
    """List the user's recent chats (1:1, group, meeting)."""
    params: Dict[str, Any] = {
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
) -> List[Dict[str, Any]]:
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
) -> Dict[str, Any]:
    """Send a message to a chat."""
    try:
        result = client.post(
            f"/chats/{_safe_id(chat_id)}/messages",
            json_data={"body": {"content": content}},
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------

async def alist_joined_teams(client: AsyncGraphClient) -> List[Dict[str, Any]]:
    """List teams the current user has joined (async)."""
    try:
        data = await client.get("/me/joinedTeams")
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


async def alist_channels(client: AsyncGraphClient, team_id: str) -> List[Dict[str, Any]]:
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
) -> Dict[str, Any]:
    """Send a message to a Teams channel (async)."""
    try:
        result = await client.post(
            f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages",
            json_data={"body": {"content": content}},
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


async def alist_channel_messages(
    client: AsyncGraphClient,
    team_id: str,
    channel_id: str,
    top: int = 20,
) -> List[Dict[str, Any]]:
    """List recent messages in a Teams channel (async)."""
    try:
        data = await client.get(
            f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages",
            params={"$top": top},
        )
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


async def alist_chats(
    client: AsyncGraphClient,
    chat_type: str = "",
    top: int = 20,
) -> List[Dict[str, Any]]:
    """List the user's recent chats (async)."""
    params: Dict[str, Any] = {
        "$top": top,
        "$expand": "lastMessagePreview,members",
        "$orderby": "lastMessagePreview/createdDateTime desc",
    }
    if chat_type:
        escaped = chat_type.replace("'", "''")
        params["$filter"] = f"chatType eq '{escaped}'"
    try:
        data = await client.get("/me/chats", params=params)
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


async def alist_chat_messages(
    client: AsyncGraphClient,
    chat_id: str,
    top: int = 20,
) -> List[Dict[str, Any]]:
    """List recent messages in a chat (async)."""
    try:
        data = await client.get(
            f"/chats/{_safe_id(chat_id)}/messages",
            params={"$top": top},
        )
    except GraphError as e:
        _check_teams_access(e)
    return data.get("value", [])


async def asend_chat_message(
    client: AsyncGraphClient,
    chat_id: str,
    content: str,
) -> Dict[str, Any]:
    """Send a message to a chat (async)."""
    try:
        result = await client.post(
            f"/chats/{_safe_id(chat_id)}/messages",
            json_data={"body": {"content": content}},
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
) -> List[Dict[str, Any]]:
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
    channel_pairs: list[tuple[str, str, Dict]] = []
    for team, ch_result in zip(teams_list, channel_results):
        if isinstance(ch_result, Exception):
            logger.warning("Failed to fetch channels for team %s: %s", team.get("displayName"), ch_result)
            continue
        for ch in ch_result:
            channel_pairs.append((team.get("displayName", "?"), team["id"], ch))

    # Cap channels
    channel_pairs = channel_pairs[:max_channels]

    # Step 3: Fetch latest message from each channel in parallel
    msg_results = await asyncio.gather(
        *[
            _safe(alist_channel_messages(client, team_id, ch["id"], top=1))
            for _, team_id, ch in channel_pairs
        ],
        return_exceptions=True,
    )

    # Build activity list
    activity: List[Dict[str, Any]] = []

    def _parse_ts(ts_str: str) -> datetime | None:
        """Parse an ISO timestamp from Graph API (handles both Z and +00:00)."""
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    # Process channel messages
    for (team_name, _, ch), msgs in zip(channel_pairs, msg_results):
        if isinstance(msgs, Exception) or not msgs:
            continue
        msg = msgs[0]
        ts = msg.get("createdDateTime", "")
        parsed = _parse_ts(ts)
        if parsed and parsed >= cutoff:
            activity.append({
                "source": "channel",
                "source_name": f"{team_name} > {ch.get('displayName', '?')}",
                "sender": extract_message_sender(msg),
                "timestamp": ts,
                "preview": extract_message_text(msg, max_length=200),
            })

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

        activity.append({
            "source": "chat",
            "source_name": source_name,
            "sender": sender,
            "timestamp": ts,
            "preview": preview_body[:200],
        })

    # Sort by timestamp descending
    activity.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return activity
