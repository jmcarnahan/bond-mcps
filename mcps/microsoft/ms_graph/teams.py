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
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from . import files as files_ops
from .attachments import ResolvedAttachment
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


class TeamsSearchUnsupportedError(Exception):
    """Raised when /search/query refuses chatMessage for this account type.

    Personal (consumer) Microsoft accounts answer 400 "not supported" — the
    Microsoft Search API is a work/school feature. This is permanent for the
    account, so callers report it rather than retrying.
    """

    def __init__(self) -> None:
        super().__init__("Teams message search is only available on work or school accounts.")


class FilesScopeMissingError(Exception):
    """Raised when a Teams file send fails because the connection cannot write files."""

    def __init__(self) -> None:
        super().__init__(
            "Sending files into Teams uploads them to OneDrive first, which needs "
            "the Files.ReadWrite permission. This connection can only read files."
        )


def _raise_scope_missing(e: GraphError) -> None:
    """Translate a 403 on the OneDrive upload into a scope problem, not a Teams one."""
    if e.status_code == 403:
        raise FilesScopeMissingError() from e
    raise e


# --- Teams message search -------------------------------------------------

SEARCH_PAGE_SIZE = 25  # hits requested per /search/query call
SEARCH_MAX_PAGES = 8  # hard stop: 8 * 25 = 200 hits examined
SEARCH_MAX_CANDIDATES = 200  # hard stop on candidates kept for hydration
SEARCH_DEFAULT_MAX_RESULTS = 25
SEARCH_MAX_RESULTS_CAP = 100
HYDRATE_CONCURRENCY = 5  # concurrent GET /chats/{id}/messages/{id}
EXACT_OVERFETCH = 2  # exact mode hydrates 2x max_results candidates

# A '#token' in the user's query. The index strips '#', so the tag is searched
# as a bare quoted term and re-checked against the message body afterwards.
HASHTAG_RE = re.compile(r"^#([A-Za-z0-9_][\w-]*)$")


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


def _plain_to_html(content: str) -> str:
    """Escape plain text and turn its newlines into line breaks."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return html_mod.escape(normalized, quote=False).replace("\n", "<br>")


def _prepare_teams_body(
    content: str,
    content_type: str = "auto",
    attachment_ids: Sequence[str] = (),
    image_count: int = 0,
) -> dict[str, str]:
    """Prepare a Teams message body dict with appropriate contentType.

    In "auto" mode (default), HTML content is detected and newlines are
    converted to <br>; plain text also has entities escaped before conversion.

    File cards and inline images are referenced from the body itself, so a
    message carrying either is always HTML — a "text" body would show the
    ``<attachment>`` and ``<img>`` tags as literal text instead of rendering
    the file card and the picture.
    """
    content_type = content_type.lower()
    if content_type not in _VALID_CONTENT_TYPES:
        raise ValueError(f"content_type must be 'auto', 'html', or 'text'; got {content_type!r}")

    has_extras = bool(attachment_ids) or image_count > 0
    if content_type == "text" and not has_extras:
        return {"contentType": "text", "content": content}

    if content_type == "html":
        html = content
    elif content_type == "text":
        html = _plain_to_html(content)
    else:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if _detect_body_type(normalized) == "HTML":
            html = normalized.replace("\n", "<br>")
        else:
            html = _plain_to_html(normalized)

    for attachment_id in attachment_ids:
        html += f'<attachment id="{attachment_id}"></attachment>'
    for index in range(1, image_count + 1):
        html += f'<img src="../hostedContents/{index}/$value">'
    return {"contentType": "html", "content": html}


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


# Inline images do not arrive as Graph ``attachments`` entries at all: the body
# HTML carries them as
# <img src="https://graph.microsoft.com/v1.0/chats/{chat}/messages/{msg}/hostedContents/{id}/$value">.
HOSTED_CONTENT_RE = re.compile(r'(https?://[^"\'<>\s]*?/hostedContents/([^/"\'<>\s]+)/\$value)')

_CARD_CONTENT_TYPE_PREFIX = "application/vnd.microsoft.card."

# Every kind parse_message_attachments can report. Documentation for callers.
ATTACHMENT_KINDS = ("file", "image", "card", "message_reference", "other")


def _str_or_none(value: Any) -> str | None:
    """Keep a non-empty string; everything else (None, ints, dicts) becomes None."""
    return value if isinstance(value, str) and value else None


def _extract_hosted_contents(msg: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered, de-duplicated (hosted_content_id, url) pairs from the body HTML.

    A message that shows the same image twice carries two identical <img> tags;
    the pair is emitted once, in the order it first appears.
    """
    body = msg.get("body")
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str) or not content:
        return []

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, hosted_id in HOSTED_CONTENT_RE.findall(content):
        if hosted_id in seen:
            continue
        seen.add(hosted_id)
        found.append((hosted_id, url))
    return found


def _attachment_kind(content_type: Any) -> str:
    """Map a Graph attachment contentType onto one of ATTACHMENT_KINDS."""
    if content_type == "reference":
        return "file"
    if isinstance(content_type, str) and content_type.startswith(_CARD_CONTENT_TYPE_PREFIX):
        return "card"
    if content_type == "messageReference":
        return "message_reference"
    return "other"


def parse_message_attachments(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """ONE unified list describing everything attached to a Teams message.

    Each entry is
    ``{"id", "kind", "name", "content_type", "content_url", "thumbnail_url", "card_text"}``.
    Graph ``attachments`` entries come first, in Graph order; inline images
    parsed out of the body HTML follow, in body order, as kind ``image`` whose
    ``id`` is the hosted content id and whose ``content_type`` is unknown until
    the bytes are fetched.

    Every level is type-checked rather than trusted: one malformed entry must
    not sink a whole page of messages, so this never raises.
    """
    entries: list[dict[str, Any]] = []

    raw = msg.get("attachments")
    if isinstance(raw, list):
        for att in raw:
            if not isinstance(att, dict):
                continue
            content_type = att.get("contentType")
            kind = _attachment_kind(content_type)
            card_text = None
            if kind == "card":
                content = att.get("content")
                if isinstance(content, str):
                    card_text = _extract_adaptive_card_text(content) or None
            entries.append(
                {
                    "id": _str_or_none(att.get("id")),
                    "kind": kind,
                    "name": _str_or_none(att.get("name")),
                    "content_type": content_type if isinstance(content_type, str) else None,
                    "content_url": _str_or_none(att.get("contentUrl")),
                    "thumbnail_url": _str_or_none(att.get("thumbnailUrl")),
                    "card_text": card_text,
                }
            )

    for hosted_id, url in _extract_hosted_contents(msg):
        entries.append(
            {
                "id": hosted_id,
                "kind": "image",
                "name": None,
                "content_type": None,
                "content_url": url,
                "thumbnail_url": None,
                "card_text": None,
            }
        )

    return entries


def _attachment_markers(msg: dict[str, Any]) -> str:
    """Trailing markers so a file-only or image-only message is not "(empty)"."""
    markers: list[str] = []
    for entry in parse_message_attachments(msg):
        if entry["kind"] == "file":
            markers.append(f"[File: {entry['name'] or '(unnamed)'}]")
        elif entry["kind"] == "image":
            markers.append("[Image]")
    return " ".join(markers)


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
        raw = msg.get("attachments")
        for att in raw if isinstance(raw, list) else []:
            if not isinstance(att, dict):
                continue
            if att.get("contentType") == "application/vnd.microsoft.card.adaptive":
                card_text = _extract_adaptive_card_text(att.get("content", ""))
                if card_text:
                    content = f"[Card] {card_text}"
                    break

    # Markers are appended AFTER truncation: max_length caps the body a person
    # wrote, not the evidence that a file came with it.
    markers = _attachment_markers(msg)

    if not content.strip():
        return markers

    if max_length > 0 and len(content) > max_length:
        content = content[:max_length] + "..."
    return f"{content} {markers}" if markers else content


def extract_message_sender(msg: dict[str, Any]) -> str:
    """Extract the display name of the message sender."""
    sender = msg.get("from") or {}
    user = sender.get("user") or {}
    app = sender.get("application") or {}
    return user.get("displayName") or app.get("displayName") or "(system)"


# ---------------------------------------------------------------------------
# Teams message search helpers (pure, shared by both twins)
# ---------------------------------------------------------------------------


def normalize_since(value: str) -> str:
    """Normalize a caller-supplied cutoff to an ISO datetime, or "" for all time.

    Same rules and wording as the inline validation in read_teams_messages:
    a bare date grows a midnight-Zulu time, an ISO datetime passes through,
    and anything else is a caller error.
    """
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value + "T00:00:00Z"
    if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
        return value
    raise ValueError(f"Invalid since format: '{value}'. Use YYYY-MM-DD or ISO datetime.")


def split_search_query(query: str) -> tuple[list[str], list[str]]:
    """Split a user query into (hashtags, other tokens).

    A token that matches HASHTAG_RE is a hashtag and is returned lowercased
    WITHOUT its '#'. Everything else passes through untouched, so KQL a user
    typed ('from:todd', 'sent>=2026-01-01') and plain keywords keep working.
    A malformed '#' token (bare '#', '#-x') is not a hashtag; it stays a
    plain token. Trailing prose punctuation ('#tag,' '#tag.') is not part of
    the tag.
    """
    hashtags: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for token in query.split():
        m = HASHTAG_RE.match(token.rstrip(".,;:!?)"))
        if m:
            tag = m.group(1).lower()
            if tag not in seen:
                seen.add(tag)
                hashtags.append(tag)
        else:
            others.append(token)
    return hashtags, others


def build_search_query_string(hashtags: list[str], others: list[str], since: str = "") -> str:
    """The queryString sent to /search/query.

    The Teams index strips '#' from indexed text, so '"#tag"' returns zero
    hits. A hashtag is therefore searched as the QUOTED bare term — quoting
    also suppresses stemming, so '"budget2026"' does not also match
    'budget20260'. A 'sent>=' KQL clause narrows the server side when a cutoff
    was given; it is day-granular, so the caller still filters timestamps
    itself.
    """
    parts = [f'"{tag}"' for tag in hashtags]
    parts.extend(others)
    if since:
        parts.append(f"sent>={since[:10]}")
    return " ".join(parts)


def hashtag_pattern(tag: str) -> re.Pattern[str]:
    """Compiled matcher for one hashtag inside message text.

    '#budget2026' must not match '#budget20260' or 'my#budget2026', so the tag
    is fenced by a lookbehind rejecting word chars and '#', and a lookahead
    rejecting word chars and '-'.
    """
    return re.compile(rf"(?<![\w#])#{re.escape(tag)}(?![\w-])", re.IGNORECASE)


def message_match_text(msg: dict[str, Any]) -> str:
    """The text a hashtag is matched against: subject + body, tags removed.

    HTML tags become a SPACE, not nothing — '<p>hi</p><p>#tag</p>' must not
    collapse to 'hi#tag', which the fenced pattern would then reject. HTML
    entities are unescaped afterwards so a body carrying '&#35;tag' still
    matches. An empty body falls back to extract_message_text so a card-only
    message is still searchable.
    """
    body = msg.get("body") or {}
    content = body.get("content") or ""
    if body.get("contentType") == "html" and content:
        content = re.sub(r"<[^>]+>", " ", content)
    text = html_mod.unescape(content)
    if not text.strip():
        text = extract_message_text(msg, max_length=-1)
    subject = msg.get("subject") or ""
    return f"{subject} {text}"


def message_has_all_hashtags(msg: dict[str, Any], hashtags: list[str]) -> bool:
    """True when EVERY hashtag appears literally in the message (AND, not OR)."""
    if not hashtags:
        return True
    text = message_match_text(msg)
    return all(hashtag_pattern(tag).search(text) for tag in hashtags)


def is_channel_hit(resource: dict[str, Any]) -> bool:
    """A channel hit carries channelIdentity.teamId; a chat hit does not.

    Verified against the tenant: for a CHAT hit channelIdentity is non-empty
    ({"channelId": <the chatId>}) with no teamId, so the presence of
    channelIdentity is NOT the discriminator — teamId is.
    """
    identity = resource.get("channelIdentity") or {}
    return bool(identity.get("teamId"))


def normalize_search_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    """One /search/query hit -> the candidate the hydrator and the CSV need.

    Returns None for a hit with no resource or no id — one malformed hit must
    not sink a page. 'chat_id' is what GET /chats/{chat_id}/messages/{id}
    takes; for a channel hit that is the channel thread id, which the same
    endpoint accepts (verified).
    """
    resource = hit.get("resource")
    if not isinstance(resource, dict):
        return None
    message_id = resource.get("id")
    if not message_id:
        return None
    identity = resource.get("channelIdentity") or {}
    channel_id = identity.get("channelId") or ""
    team_id = identity.get("teamId") or ""
    chat_id = resource.get("chatId") or channel_id
    channel = is_channel_hit(resource)
    return {
        "id": str(message_id),
        "chat_id": str(chat_id),
        "team_id": str(team_id),
        "channel_id": str(channel_id),
        "is_channel": channel,
        "created": resource.get("createdDateTime") or "",
        "web_link": resource.get("webLink") or "",
        "conversation": (f"channel:{team_id}/{channel_id}" if channel else f"chat:{chat_id}"),
    }


def hit_matches_conversation(candidate: dict[str, Any], conversation_id: str) -> bool:
    """Client-side scope filter: the Teams index cannot scope by chat/channel.

    Matches the candidate's chatId OR its channelIdentity.channelId, so the
    caller may pass either a chat id (19:...@unq.gbl.spaces / @thread.v2) or a
    channel id (19:...@thread.tacv2).
    """
    if not conversation_id:
        return True
    wanted = conversation_id.strip().casefold()
    return wanted in {
        candidate["chat_id"].casefold(),
        candidate["channel_id"].casefold(),
    }


def _search_payload(query_string: str, offset: int) -> dict[str, Any]:
    """The POST /search/query body for one page of chatMessage hits."""
    return {
        "requests": [
            {
                "entityTypes": ["chatMessage"],
                "query": {"queryString": query_string},
                "from": offset,
                "size": SEARCH_PAGE_SIZE,
            }
        ]
    }


def _search_container(data: dict[str, Any] | None) -> dict[str, Any]:
    """The first hitsContainer of a search response, or {}.

    Deliberately NOT files._parse_search_response: that one injects
    _searchSummary and throws away moreResultsAvailable, which is the only
    paging signal Teams search gives (its 'total' is per-page and must never
    be surfaced).
    """
    for entry in (data or {}).get("value") or []:
        for container in entry.get("hitsContainers") or []:
            if isinstance(container, dict):
                return container
    return {}


def _search_error(e: GraphError) -> None:
    """Translate a /search/query failure, then re-raise."""
    if e.status_code == 400 and "not supported" in str(e).lower():
        raise TeamsSearchUnsupportedError() from e
    _check_teams_access(e)  # 403 -> TeamsNotAvailableError; else re-raise


def _collect(
    container: dict[str, Any],
    candidates: list[dict[str, Any]],
    seen: set[str],
    conversation_id: str,
    since: str,
    budget: int,
) -> bool:
    """Fold one page's hits into `candidates`. Returns True when full.

    Filters BEFORE counting toward the budget, so a conversation_id scope
    keeps paging until enough scoped hits exist or the index is exhausted.
    Dedupes by message id: the index can repeat a hit across pages when it
    shifts under us.
    """
    for hit in container.get("hits") or []:
        candidate = normalize_search_hit(hit if isinstance(hit, dict) else {})
        if candidate is None or candidate["id"] in seen:
            continue
        if not hit_matches_conversation(candidate, conversation_id):
            continue
        if since and candidate["created"] and candidate["created"] < since:
            # 'sent>=' KQL is day-granular; this honours a datetime cutoff.
            continue
        seen.add(candidate["id"])
        candidates.append(candidate)
        if len(candidates) >= budget:
            return True
    return False


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------


def _message_base(chat_id: str = "", team_id: str = "", channel_id: str = "") -> str:
    """The messages collection for a chat or a channel, with IDs percent-encoded.

    Raises ValueError when neither target or both targets are given — the
    caller must decide which conversation it means.
    """
    if chat_id and (team_id or channel_id):
        raise ValueError("Provide chat_id or team_id and channel_id, not both.")
    if chat_id:
        return f"/chats/{_safe_id(chat_id)}/messages"
    if team_id and channel_id:
        return f"/teams/{_safe_id(team_id)}/channels/{_safe_id(channel_id)}/messages"
    raise ValueError("Provide chat_id, or both team_id and channel_id.")


def get_message(
    client: GraphClient,
    message_id: str,
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
) -> dict[str, Any]:
    """Fetch ONE chat or channel message. Top-level messages only, not replies."""
    base = _message_base(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    try:
        return client.get(f"{base}/{_safe_id(message_id)}")
    except GraphError as e:
        _check_teams_access(e)


def get_hosted_content(
    client: GraphClient,
    message_id: str,
    hosted_content_id: str,
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
) -> tuple[bytes, str]:
    """Raw bytes plus Content-Type of one inline image.

    Only ``$value`` carries the bytes — the JSON form returns a null
    ``contentBytes``.
    """
    base = _message_base(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    path = f"{base}/{_safe_id(message_id)}/hostedContents/{_safe_id(hosted_content_id)}/$value"
    try:
        return client.get_bytes_with_type(path)
    except GraphError as e:
        _check_teams_access(e)


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
    attachments: list[dict[str, Any]] | None = None,
    hosted_contents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a Teams channel."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    if attachments:
        payload["attachments"] = attachments
    if hosted_contents:
        payload["hostedContents"] = hosted_contents
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


def search_messages(
    client: GraphClient,
    query: str,
    since: str = "",
    conversation_id: str = "",
    max_results: int = SEARCH_DEFAULT_MAX_RESULTS,
    exact: bool | None = None,
) -> dict[str, Any]:
    """Sync twin of asearch_messages, for the CLI.

    Hydration is sequential — a CLI search of 25 hits is 25 GETs in a row,
    which is fine at a human's pace and keeps the CLI free of asyncio.
    """
    hashtags, others = split_search_query(query)
    if exact is None:
        exact = bool(hashtags)
    max_results = max(1, min(max_results or SEARCH_DEFAULT_MAX_RESULTS, SEARCH_MAX_RESULTS_CAP))
    query_string = build_search_query_string(hashtags, others, since)
    empty: dict[str, Any] = {
        "messages": [],
        "hashtags": hashtags,
        "exact": exact,
        "query_string": query_string,
        "candidates": 0,
        "skipped": 0,
        "truncated": False,
    }
    if not query_string.strip():
        return empty

    budget = min(
        max_results * EXACT_OVERFETCH if (exact and hashtags) else max_results,
        SEARCH_MAX_CANDIDATES,
    )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    truncated = False
    for page in range(SEARCH_MAX_PAGES):
        try:
            data = client.post("/search/query", json_data=_search_payload(query_string, offset))
        except GraphError as e:
            _search_error(e)
        container = _search_container(data)
        hits = container.get("hits") or []
        full = _collect(container, candidates, seen, conversation_id, since, budget)
        offset += len(hits)
        if full:
            truncated = True  # more may exist behind the budget
            break
        if not hits or not container.get("moreResultsAvailable"):
            break
        if page == SEARCH_MAX_PAGES - 1:
            truncated = True
    if not candidates:
        return {**empty, "truncated": truncated}

    messages: list[dict[str, Any]] = []
    skipped = 0
    errors: list[BaseException] = []
    for candidate in candidates:
        try:
            msg = get_message(client, candidate["id"], chat_id=candidate["chat_id"])
        except Exception as e:  # noqa: BLE001 — one bad id must not sink the search
            skipped += 1
            errors.append(e)
            logger.warning("Teams search: skipping unreadable message: %s", e)
            continue
        msg["_conversation"] = candidate["conversation"]
        msg["_web_link"] = candidate["web_link"]
        messages.append(msg)
    # Every hydration answered 403 => not "one deleted message" but a withdrawn
    # licence or scope (_check_teams_access already turned each 403 into
    # TeamsNotAvailableError). Anything else — a lone deleted hit, a 5xx — is
    # skipped and counted, so the caller sees "0 results, N skipped".
    if not messages and errors and all(isinstance(e, TeamsNotAvailableError) for e in errors):
        raise errors[0]

    if exact and hashtags:
        messages = [m for m in messages if message_has_all_hashtags(m, hashtags)]
    if len(messages) > max_results:
        messages = messages[:max_results]
        truncated = True
    return {
        "messages": messages,
        "hashtags": hashtags,
        "exact": exact,
        "query_string": query_string,
        "candidates": len(candidates),
        "skipped": skipped,
        "truncated": truncated,
    }


def send_chat_message(
    client: GraphClient,
    chat_id: str,
    content: str,
    content_type: str = "auto",
    mentions: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    hosted_contents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a chat."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    if attachments:
        payload["attachments"] = attachments
    if hosted_contents:
        payload["hostedContents"] = hosted_contents
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


async def aget_message(
    client: AsyncGraphClient,
    message_id: str,
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
) -> dict[str, Any]:
    """Fetch ONE chat or channel message (async)."""
    base = _message_base(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    try:
        return await client.get(f"{base}/{_safe_id(message_id)}")
    except GraphError as e:
        _check_teams_access(e)


async def aget_hosted_content(
    client: AsyncGraphClient,
    message_id: str,
    hosted_content_id: str,
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
) -> tuple[bytes, str]:
    """Raw bytes plus Content-Type of one inline image (async)."""
    base = _message_base(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    path = f"{base}/{_safe_id(message_id)}/hostedContents/{_safe_id(hosted_content_id)}/$value"
    try:
        return await client.get_bytes_with_type(path)
    except GraphError as e:
        _check_teams_access(e)


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
    attachments: list[dict[str, Any]] | None = None,
    hosted_contents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a Teams channel (async)."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    if attachments:
        payload["attachments"] = attachments
    if hosted_contents:
        payload["hostedContents"] = hosted_contents
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


async def asearch_messages(
    client: AsyncGraphClient,
    query: str,
    since: str = "",
    conversation_id: str = "",
    max_results: int = SEARCH_DEFAULT_MAX_RESULTS,
    exact: bool | None = None,
) -> dict[str, Any]:
    """Search Teams chats and channels for messages, then hydrate the hits.

    /search/query returns metadata only — no body, and 'fields' cannot add one
    — so every hit is fetched again through GET /chats/{chatId}/messages/{id},
    which serves channel messages too (a channel hit's chatId is its channel
    thread id).

    Args:
        query: raw user query. '#tags' are extracted and matched exactly after
            hydration; everything else is passed to the index as-is.
        since: normalized ISO datetime cutoff, or "" for all time.
        conversation_id: chat id or channel id to scope to, client-side.
        max_results: messages to return, capped at SEARCH_MAX_RESULTS_CAP.
        exact: None (default) means "exact when the query has hashtags".

    Returns:
        {"messages": [...], "hashtags": [...], "exact": bool,
         "query_string": str, "candidates": int, "skipped": int,
         "truncated": bool}
        Each message is the full Graph chatMessage with the candidate's
        '_conversation' and '_web_link' merged in for the caller to render.

    Raises:
        TeamsSearchUnsupportedError: consumer account.
        TeamsNotAvailableError: 403 on search, or every hydration failed with
            403 (no Teams licence / scope withdrawn).
    """
    hashtags, others = split_search_query(query)
    if exact is None:
        exact = bool(hashtags)
    max_results = max(1, min(max_results or SEARCH_DEFAULT_MAX_RESULTS, SEARCH_MAX_RESULTS_CAP))
    query_string = build_search_query_string(hashtags, others, since)
    empty: dict[str, Any] = {
        "messages": [],
        "hashtags": hashtags,
        "exact": exact,
        "query_string": query_string,
        "candidates": 0,
        "skipped": 0,
        "truncated": False,
    }
    if not query_string.strip():
        return empty

    budget = min(
        max_results * EXACT_OVERFETCH if (exact and hashtags) else max_results,
        SEARCH_MAX_CANDIDATES,
    )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    truncated = False
    for page in range(SEARCH_MAX_PAGES):
        try:
            data = await client.post(
                "/search/query", json_data=_search_payload(query_string, offset)
            )
        except GraphError as e:
            _search_error(e)
        container = _search_container(data)
        hits = container.get("hits") or []
        full = _collect(container, candidates, seen, conversation_id, since, budget)
        offset += len(hits)
        if full:
            truncated = True  # more may exist behind the budget
            break
        if not hits or not container.get("moreResultsAvailable"):
            break
        if page == SEARCH_MAX_PAGES - 1:
            truncated = True
    if not candidates:
        return {**empty, "truncated": truncated}

    sem = asyncio.Semaphore(HYDRATE_CONCURRENCY)

    async def _hydrate(candidate: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            msg = await aget_message(client, candidate["id"], chat_id=candidate["chat_id"])
        msg["_conversation"] = candidate["conversation"]
        msg["_web_link"] = candidate["web_link"]
        return msg

    results = await asyncio.gather(*[_hydrate(c) for c in candidates], return_exceptions=True)
    messages: list[dict[str, Any]] = []
    skipped = 0
    errors: list[BaseException] = []
    for item in results:
        if isinstance(item, BaseException):
            skipped += 1
            errors.append(item)
            logger.warning("Teams search: skipping unreadable message: %s", item)
            continue
        messages.append(item)
    # Every hydration answered 403 => not "one deleted message" but a withdrawn
    # licence or scope (_check_teams_access already turned each 403 into
    # TeamsNotAvailableError). Anything else — a lone deleted hit, a 5xx — is
    # skipped and counted, so the caller sees "0 results, N skipped".
    if not messages and errors and all(isinstance(e, TeamsNotAvailableError) for e in errors):
        raise errors[0]

    if exact and hashtags:
        messages = [m for m in messages if message_has_all_hashtags(m, hashtags)]
    if len(messages) > max_results:
        messages = messages[:max_results]
        truncated = True
    return {
        "messages": messages,
        "hashtags": hashtags,
        "exact": exact,
        "query_string": query_string,
        "candidates": len(candidates),
        "skipped": skipped,
        "truncated": truncated,
    }


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
    attachments: list[dict[str, Any]] | None = None,
    hosted_contents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a chat (async)."""
    body = _prepare_teams_body(content, content_type)
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    if attachments:
        payload["attachments"] = attachments
    if hosted_contents:
        payload["hostedContents"] = hosted_contents
    try:
        result = await client.post(
            f"/chats/{_safe_id(chat_id)}/messages",
            json_data=payload,
        )
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


# ---------------------------------------------------------------------------
# Sending files and inline images
#
# Teams does not accept file bytes on a message. A file is uploaded to a drive
# first (a chat: the sender's OneDrive; a channel: the channel's Files folder),
# and the message then carries a "reference" attachment pointing at it plus an
# <attachment> tag in the body. Small pictures can instead ride along as
# hostedContents and render inline.
# ---------------------------------------------------------------------------

# The attachment id Teams expects is the GUID inside the driveItem's eTag,
# which arrives wrapped as '"{GUID},N"'.
ETAG_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Teams itself puts chat files here, and members are given read access per file.
TEAMS_CHAT_FILES_FOLDER = "Microsoft Teams Chat Files"

# hostedContents travel base64 inside the message payload, so they stay small.
MAX_HOSTED_IMAGE_BYTES = 4_000_000


def _attachment_from_drive_item(item: dict[str, Any]) -> dict[str, Any]:
    """The Graph attachment entry that turns an uploaded file into a Teams card."""
    name = item.get("name") or ""
    match = ETAG_GUID_RE.search(str(item.get("eTag") or ""))
    if not match:
        raise ValueError(f"Uploaded file {name!r} has no eTag GUID; Teams cannot reference it.")
    return {
        "id": match.group(0),
        "contentType": "reference",
        "contentUrl": item.get("webDavUrl") or item.get("webUrl") or "",
        "name": name,
    }


def _member_emails(members: dict[str, Any], exclude_user_id: str = "") -> list[str]:
    """Addressable e-mails from a chat member list, de-duplicated case-insensitively.

    ``exclude_user_id`` is the signed-in user's AAD object id (the token's
    ``oid`` claim): the sender already owns the file, and inviting the owner
    is at best a no-op and at worst the one recipient that makes Graph reject
    the whole invite.
    """
    emails: list[str] = []
    seen: set[str] = set()
    for member in members.get("value", []) or []:
        if not isinstance(member, dict):
            continue
        if exclude_user_id and member.get("userId") == exclude_user_id:
            continue
        raw = member.get("email")
        if not isinstance(raw, str):
            continue
        email = raw.strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        emails.append(email)
    return emails


def _hosted_contents(images: Sequence[ResolvedAttachment]) -> list[dict[str, Any]]:
    """Inline-image payload entries, numbered from 1 to match the body's <img> tags."""
    hosted: list[dict[str, Any]] = []
    for index, image in enumerate(images, 1):
        if not image.content_type.startswith("image/"):
            raise ValueError(f"Inline image {image.name!r} is {image.content_type}, not an image")
        if len(image.data) > MAX_HOSTED_IMAGE_BYTES:
            raise ValueError(
                f"Inline image {image.name!r} is {len(image.data)} bytes; "
                f"the limit is {MAX_HOSTED_IMAGE_BYTES:,}"
            )
        hosted.append(
            {
                "@microsoft.graph.temporaryId": str(index),
                "contentBytes": base64.b64encode(image.data).decode("ascii"),
                "contentType": image.content_type,
            }
        )
    return hosted


def _channel_folder_target(folder: dict[str, Any]) -> tuple[str, str]:
    """(drive_id, parent_item_id) for a channel's Files folder."""
    try:
        return folder["parentReference"]["driveId"], folder["id"]
    except (KeyError, TypeError) as e:
        raise GraphError(500, "NoFilesFolder", "Channel files folder has no drive") from e


def upload_chat_file(
    client: GraphClient,
    chat_id: str,
    att: ResolvedAttachment,
    exclude_user_id: str = "",
) -> dict[str, Any]:
    """Put one file where a chat can reference it, and let the chat read it.

    The re-fetch is not redundant: an upload response carries no webDavUrl, and
    that is the URL a Teams file card needs. A failed share — whether listing
    the members or granting them access — is logged, never fatal: members who
    already have access still see the file, and the upload has already happened.
    """
    try:
        item = files_ops.upload_any(
            client,
            TEAMS_CHAT_FILES_FOLDER,
            att.name,
            att.data,
            att.content_type,
            conflict_behavior="rename",
        )
    except GraphError as e:
        _raise_scope_missing(e)
    item = files_ops.get_drive_item(client, item["id"], select=files_ops.TEAMS_ITEM_SELECT)
    try:
        emails = _member_emails(chat_members(client, chat_id), exclude_user_id)
        files_ops.invite_drive_item(client, item["id"], emails)
    except GraphError as e:
        logger.warning("Could not share %s with chat members: %s", att.name, e)
    return item


async def aupload_chat_file(
    client: AsyncGraphClient,
    chat_id: str,
    att: ResolvedAttachment,
    exclude_user_id: str = "",
) -> dict[str, Any]:
    """Put one file where a chat can reference it (async). See upload_chat_file."""
    try:
        item = await files_ops.aupload_any(
            client,
            TEAMS_CHAT_FILES_FOLDER,
            att.name,
            att.data,
            att.content_type,
            conflict_behavior="rename",
        )
    except GraphError as e:
        _raise_scope_missing(e)
    item = await files_ops.aget_drive_item(client, item["id"], select=files_ops.TEAMS_ITEM_SELECT)
    try:
        emails = _member_emails(await achat_members(client, chat_id), exclude_user_id)
        await files_ops.ainvite_drive_item(client, item["id"], emails)
    except GraphError as e:
        logger.warning("Could not share %s with chat members: %s", att.name, e)
    return item


def upload_channel_file(
    client: GraphClient,
    team_id: str,
    channel_id: str,
    att: ResolvedAttachment,
) -> dict[str, Any]:
    """Put one file in a channel's Files folder, where the whole team can read it."""
    try:
        folder = files_ops.get_channel_files_folder(client, team_id, channel_id)
    except GraphError as e:
        _check_teams_access(e)
    drive_id, parent_id = _channel_folder_target(folder)
    try:
        item = files_ops.upload_any(
            client,
            "",
            att.name,
            att.data,
            att.content_type,
            drive_id=drive_id,
            parent_id=parent_id,
            conflict_behavior="rename",
        )
    except GraphError as e:
        _raise_scope_missing(e)
    return files_ops.get_drive_item(
        client, item["id"], drive_id=drive_id, select=files_ops.TEAMS_ITEM_SELECT
    )


async def aupload_channel_file(
    client: AsyncGraphClient,
    team_id: str,
    channel_id: str,
    att: ResolvedAttachment,
) -> dict[str, Any]:
    """Put one file in a channel's Files folder (async). See upload_channel_file."""
    try:
        folder = await files_ops.aget_channel_files_folder(client, team_id, channel_id)
    except GraphError as e:
        _check_teams_access(e)
    drive_id, parent_id = _channel_folder_target(folder)
    try:
        item = await files_ops.aupload_any(
            client,
            "",
            att.name,
            att.data,
            att.content_type,
            drive_id=drive_id,
            parent_id=parent_id,
            conflict_behavior="rename",
        )
    except GraphError as e:
        _raise_scope_missing(e)
    return await files_ops.aget_drive_item(
        client, item["id"], drive_id=drive_id, select=files_ops.TEAMS_ITEM_SELECT
    )


def _files_payload(
    content: str,
    content_type: str,
    mentions: list[dict[str, Any]] | None,
    attachments: list[dict[str, Any]],
    hosted: list[dict[str, Any]],
) -> dict[str, Any]:
    """The message payload for a send that carries files, images, or both."""
    body = _prepare_teams_body(
        content,
        content_type,
        attachment_ids=[a["id"] for a in attachments],
        image_count=len(hosted),
    )
    payload: dict[str, Any] = {"body": body}
    if mentions:
        payload["mentions"] = mentions
    if attachments:
        payload["attachments"] = attachments
    if hosted:
        payload["hostedContents"] = hosted
    return payload


def _uploaded_item_path(item: dict[str, Any]) -> str:
    """Where an uploaded driveItem lives, for cleanup: its own drive when known."""
    parent = item.get("parentReference")
    drive_id = parent.get("driveId") if isinstance(parent, dict) else None
    return f"{files_ops._drive_base(None, drive_id or None)}/items/{item['id']}"


def _discard_uploads(client: GraphClient, items: list[dict[str, Any]]) -> None:
    """Best-effort removal of files uploaded for a message that never posted.

    Without this a retry uploads the same file again under a renamed copy. The
    original failure is what the caller must see, so nothing here raises.
    """
    for item in items:
        try:
            client.delete(_uploaded_item_path(item))
        except Exception:
            logger.warning("Could not remove uploaded file %s after a failed send", item.get("id"))


async def _adiscard_uploads(client: AsyncGraphClient, items: list[dict[str, Any]]) -> None:
    """Best-effort removal of orphaned uploads (async)."""
    for item in items:
        try:
            await client.delete(_uploaded_item_path(item))
        except Exception:
            logger.warning("Could not remove uploaded file %s after a failed send", item.get("id"))


def send_message_with_files(
    client: GraphClient,
    *,
    content: str,
    content_type: str = "auto",
    mentions: list[dict[str, Any]] | None = None,
    files: Sequence[ResolvedAttachment] = (),
    images: Sequence[ResolvedAttachment] = (),
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
    exclude_user_id: str = "",
) -> dict[str, Any]:
    """Send one message carrying uploaded files and/or inline images.

    The target and the images are validated before anything is uploaded, so a
    bad request costs no writes. If a later upload or the post itself fails,
    the files already uploaded are removed again (best effort) so a retry does
    not leave renamed duplicates behind. ``exclude_user_id`` is the sender's
    AAD object id, kept out of the chat share list.
    """
    base = _message_base(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    hosted = _hosted_contents(images)
    items: list[dict[str, Any]] = []
    try:
        for att in files:
            if chat_id:
                items.append(upload_chat_file(client, chat_id, att, exclude_user_id))
            else:
                items.append(upload_channel_file(client, team_id, channel_id, att))
        attachments = [_attachment_from_drive_item(item) for item in items]
        payload = _files_payload(content, content_type, mentions, attachments, hosted)
        try:
            result = client.post(base, json_data=payload)
        except GraphError as e:
            _check_teams_access(e)
    except Exception:
        _discard_uploads(client, items)
        raise
    return result or {}


async def asend_message_with_files(
    client: AsyncGraphClient,
    *,
    content: str,
    content_type: str = "auto",
    mentions: list[dict[str, Any]] | None = None,
    files: Sequence[ResolvedAttachment] = (),
    images: Sequence[ResolvedAttachment] = (),
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
    exclude_user_id: str = "",
) -> dict[str, Any]:
    """Send one message carrying files and/or inline images (async).

    See send_message_with_files for the cleanup and exclude_user_id rules.
    """
    base = _message_base(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    hosted = _hosted_contents(images)
    items: list[dict[str, Any]] = []
    try:
        for att in files:
            if chat_id:
                items.append(await aupload_chat_file(client, chat_id, att, exclude_user_id))
            else:
                items.append(await aupload_channel_file(client, team_id, channel_id, att))
        attachments = [_attachment_from_drive_item(item) for item in items]
        payload = _files_payload(content, content_type, mentions, attachments, hosted)
        try:
            result = await client.post(base, json_data=payload)
        except GraphError as e:
            _check_teams_access(e)
    except Exception:
        await _adiscard_uploads(client, items)
        raise
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


# ---------------------------------------------------------------------------
# Chat creation (desktop: message a person who has no chat yet)
# ---------------------------------------------------------------------------

_CHAT_MEMBER_TYPE = "#microsoft.graph.aadUserConversationMember"


def _chat_create_payload(member_ids: list[str], topic: str = "") -> dict[str, Any]:
    """Body for POST /chats. Two members (the caller and one other) make a
    oneOnOne chat, which Graph de-duplicates; more make a group chat, which it
    never does. topic is a group-only property, so it is sent only then."""
    payload: dict[str, Any] = {
        "chatType": "oneOnOne" if len(member_ids) == 2 else "group",
        "members": [
            {
                "@odata.type": _CHAT_MEMBER_TYPE,
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{member_id}')",
            }
            for member_id in member_ids
        ],
    }
    if payload["chatType"] == "group" and topic:
        payload["topic"] = topic
    return payload


def create_chat(client: GraphClient, member_ids: list[str], topic: str = "") -> dict[str, Any]:
    """Create a chat (or, for a 1:1, get the existing one). member_ids go into
    users('…') verbatim; the caller validates them. Needs Chat.ReadWrite."""
    try:
        result = client.post("/chats", json_data=_chat_create_payload(member_ids, topic))
    except GraphError as e:
        _check_teams_access(e)
    return result or {}


async def acreate_chat(
    client: AsyncGraphClient, member_ids: list[str], topic: str = ""
) -> dict[str, Any]:
    """Create a chat (or, for a 1:1, get the existing one) (async)."""
    try:
        result = await client.post("/chats", json_data=_chat_create_payload(member_ids, topic))
    except GraphError as e:
        _check_teams_access(e)
    return result or {}
