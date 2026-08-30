"""
Mail operations using the Microsoft Graph API.

All functions accept a GraphClient or AsyncGraphClient and return parsed dicts.
"""

import logging
import re
from typing import Any
from urllib.parse import quote, unquote

from .graph_client import AsyncGraphClient, GraphClient, GraphError
from .pagination import apaginate

logger = logging.getLogger(__name__)


def _safe_id(value: str) -> str:
    """URL-encode an ID to prevent path traversal in Graph API URLs.

    Graph message IDs are base64url-ish and routinely contain ``/`` and ``+``,
    both of which change the meaning of a path segment if interpolated raw.
    """
    return quote(value, safe="")


# Whitelist of standard HTML element names. Using a whitelist (rather than
# matching any word after <) prevents false positives on patterns like
# "Dear <FirstName>," or "if x<y and z>w" being mis-classified as HTML.
_HTML_TAG_NAMES = frozenset(
    {
        "html",
        "head",
        "body",
        "div",
        "span",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "nav",
        "aside",
        "pre",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "br",
        "hr",
        "wbr",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "colgroup",
        "col",
        "a",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "del",
        "ins",
        "mark",
        "small",
        "code",
        "kbd",
        "samp",
        "sup",
        "sub",
        "abbr",
        "img",
        "figure",
        "figcaption",
        "picture",
        "source",
        "form",
        "input",
        "button",
        "label",
        "select",
        "option",
        "textarea",
        "script",
        "style",
        "link",
        "meta",
        "title",
        "font",
        "center",
        "nobr",
    }
)
_HTML_TAG_RE = re.compile(
    r"</?(?:" + "|".join(sorted(_HTML_TAG_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_VALID_BODY_TYPES = frozenset({"HTML", "Text", "auto"})


def _base(mailbox: str | None) -> str:
    """Graph API path prefix: /users/{mailbox} for shared, /me for own."""
    if mailbox:
        return f"/users/{quote(mailbox, safe='@')}"
    return "/me"


def _detect_body_type(body: str) -> str:
    """Detect whether a body string is HTML or plain text.

    Searches for known HTML element names to classify the body. A whitelist
    avoids false positives on patterns like 'Dear <FirstName>,' or
    'if x<y and z>w' that contain angle brackets but are not HTML.
    """
    return "HTML" if _HTML_TAG_RE.search(body) else "Text"


def _extract_mailbox_address(odata_context: str) -> str | None:
    """Extract the mailbox email from an @odata.context URL.

    For consumer accounts, the /me/mailboxSettings @odata.context contains the
    real mailbox address (e.g. ``user@outlook.com``) even when /me returns the
    external login email (e.g. ``user@gmail.com``).
    """
    match = re.search(r"users\('([^']+)'\)", odata_context)
    if match:
        return unquote(match.group(1))
    return None


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------


def get_profile(client: GraphClient) -> dict[str, Any]:
    """Get the authenticated user's profile from /me.

    Also attempts to fetch /me/mailboxSettings to discover the real mailbox
    address (important for consumer accounts where /me returns the login email).
    If MailboxSettings.Read scope is available, adds a ``mailboxAddress`` field.
    """
    profile = client.get("/me")
    try:
        settings = client.get("/me/mailboxSettings")
        context = settings.get("@odata.context", "")
        mailbox_addr = _extract_mailbox_address(context)
        if mailbox_addr:
            profile["mailboxAddress"] = mailbox_addr
    except GraphError:
        logger.debug("Could not fetch mailboxSettings (scope may not be granted)")
    return profile


async def aget_profile(client: AsyncGraphClient) -> dict[str, Any]:
    """Get the authenticated user's profile from /me (async).

    Also attempts to fetch /me/mailboxSettings to discover the real mailbox
    address (important for consumer accounts where /me returns the login email).
    If MailboxSettings.Read scope is available, adds a ``mailboxAddress`` field.
    """
    profile = await client.get("/me")
    try:
        settings = await client.get("/me/mailboxSettings")
        context = settings.get("@odata.context", "")
        mailbox_addr = _extract_mailbox_address(context)
        if mailbox_addr:
            profile["mailboxAddress"] = mailbox_addr
    except GraphError:
        logger.debug("Could not fetch mailboxSettings (scope may not be granted)")
    return profile


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------


def list_messages(
    client: GraphClient,
    folder: str = "inbox",
    top: int = 10,
    mailbox: str | None = None,
) -> list[dict[str, Any]]:
    """List recent messages in a mail folder.

    ``folder`` may be a well-known name or a real folder ID; real IDs contain
    reserved characters (=, /, +) and must be percent-encoded.
    """
    data = client.get(
        f"{_base(mailbox)}/mailFolders/{quote(folder, safe='')}/messages",
        params={"$top": top, "$orderby": "receivedDateTime desc"},
    )
    return data.get("value", [])


def get_message(client: GraphClient, message_id: str, mailbox: str | None = None) -> dict[str, Any]:
    """Get a single message by ID."""
    return client.get(f"{_base(mailbox)}/messages/{message_id}")


def send_message(
    client: GraphClient,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    from_address: str | None = None,
    body_type: str = "auto",
    mailbox: str | None = None,
) -> None:
    """Send an email message."""
    if body_type not in _VALID_BODY_TYPES:
        raise ValueError(f"body_type must be 'HTML', 'Text', or 'auto'; got {body_type!r}")
    to_recipients = [{"emailAddress": {"address": addr}} for addr in to]
    cc_recipients = [{"emailAddress": {"address": addr}} for addr in (cc or [])]
    bcc_recipients = [{"emailAddress": {"address": addr}} for addr in (bcc or [])]
    effective_type = _detect_body_type(body) if body_type == "auto" else body_type

    payload: dict[str, Any] = {
        "message": {
            "subject": subject,
            "body": {"contentType": effective_type, "content": body},
            "toRecipients": to_recipients,
        },
        "saveToSentItems": True,
    }
    if cc_recipients:
        payload["message"]["ccRecipients"] = cc_recipients
    if bcc_recipients:
        payload["message"]["bccRecipients"] = bcc_recipients
    if from_address:
        payload["message"]["from"] = {"emailAddress": {"address": from_address}}

    client.post(f"{_base(mailbox)}/sendMail", json_data=payload)


def search_messages(
    client: GraphClient,
    query: str,
    top: int = 10,
    mailbox: str | None = None,
) -> list[dict[str, Any]]:
    """Search messages using KQL query syntax."""
    data = client.get(
        f"{_base(mailbox)}/messages",
        params={"$search": f'"{query}"', "$top": top},
    )
    return data.get("value", [])


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------


async def alist_messages(
    client: AsyncGraphClient,
    folder: str = "inbox",
    top: int = 10,
    select: str | None = None,
    mailbox: str | None = None,
) -> list[dict[str, Any]]:
    """List recent messages in a mail folder (async), paginating if needed.

    ``folder`` may be a well-known name (inbox, ...) or a real folder ID; real
    IDs contain reserved characters (=, /, +) and must be percent-encoded.
    """
    params: dict[str, Any] = {"$orderby": "receivedDateTime desc"}
    if select:
        params["$select"] = select
    path = f"{_base(mailbox)}/mailFolders/{quote(folder, safe='')}/messages"
    return await apaginate(client, path, params, top, page_size=999)


async def aget_message(
    client: AsyncGraphClient, message_id: str, mailbox: str | None = None
) -> dict[str, Any]:
    """Get a single message by ID (async)."""
    return await client.get(f"{_base(mailbox)}/messages/{message_id}")


async def amark_read(
    client: AsyncGraphClient, message_id: str, is_read: bool = True, mailbox: str | None = None
) -> dict[str, Any]:
    """Mark a message as read (or unread) and return the updated message (async)."""
    return await client.patch(
        f"{_base(mailbox)}/messages/{message_id}", json_data={"isRead": is_read}
    )


# ---------------------------------------------------------------------------
# Inbox rules (messageRules)
# ---------------------------------------------------------------------------

_RULES_PATH = "/me/mailFolders/inbox/messageRules"


async def alist_inbox_rules(client: AsyncGraphClient) -> list[dict[str, Any]]:
    """List the inbox rules (messageRules) for the mailbox (async)."""
    data = await client.get(_RULES_PATH)
    return data.get("value", [])


async def aget_inbox_rule(client: AsyncGraphClient, rule_id: str) -> dict[str, Any]:
    """Get a single inbox rule by ID (async)."""
    return await client.get(f"{_RULES_PATH}/{quote(rule_id, safe='')}")


async def acreate_inbox_rule(client: AsyncGraphClient, rule: dict[str, Any]) -> dict[str, Any]:
    """Create an inbox rule and return the created rule (async).

    The Graph-shaped ``rule`` must include displayName, sequence, and actions.
    """
    return await client.post(_RULES_PATH, json_data=rule)


async def aupdate_inbox_rule(
    client: AsyncGraphClient, rule_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Apply a partial update to an inbox rule and return the updated rule (async)."""
    return await client.patch(f"{_RULES_PATH}/{quote(rule_id, safe='')}", json_data=changes)


async def adelete_inbox_rule(client: AsyncGraphClient, rule_id: str) -> None:
    """Delete an inbox rule by ID (async)."""
    await client.delete(f"{_RULES_PATH}/{quote(rule_id, safe='')}")


async def asend_message(
    client: AsyncGraphClient,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    from_address: str | None = None,
    body_type: str = "auto",
    mailbox: str | None = None,
) -> None:
    """Send an email message (async)."""
    if body_type not in _VALID_BODY_TYPES:
        raise ValueError(f"body_type must be 'HTML', 'Text', or 'auto'; got {body_type!r}")
    to_recipients = [{"emailAddress": {"address": addr}} for addr in to]
    cc_recipients = [{"emailAddress": {"address": addr}} for addr in (cc or [])]
    bcc_recipients = [{"emailAddress": {"address": addr}} for addr in (bcc or [])]
    effective_type = _detect_body_type(body) if body_type == "auto" else body_type

    payload: dict[str, Any] = {
        "message": {
            "subject": subject,
            "body": {"contentType": effective_type, "content": body},
            "toRecipients": to_recipients,
        },
        "saveToSentItems": True,
    }
    if cc_recipients:
        payload["message"]["ccRecipients"] = cc_recipients
    if bcc_recipients:
        payload["message"]["bccRecipients"] = bcc_recipients
    if from_address:
        payload["message"]["from"] = {"emailAddress": {"address": from_address}}

    await client.post(f"{_base(mailbox)}/sendMail", json_data=payload)


async def asearch_messages(
    client: AsyncGraphClient,
    query: str,
    top: int = 10,
    select: str | None = None,
    mailbox: str | None = None,
    folder: str | None = None,
) -> list[dict[str, Any]]:
    """Search messages using KQL query syntax (async), paginating if needed.

    When ``folder`` (a real folder ID or well-known name) is given, the search is
    scoped to that folder; otherwise it runs across all folders. Graph forbids
    combining ``$search`` with ``$orderby``, so no ordering is requested here.
    """
    params: dict[str, Any] = {"$search": f'"{query}"'}
    if select:
        params["$select"] = select
    if folder:
        path = f"{_base(mailbox)}/mailFolders/{quote(folder, safe='')}/messages"
    else:
        path = f"{_base(mailbox)}/messages"
    return await apaginate(client, path, params, top, page_size=250)


# ---------------------------------------------------------------------------
# Desktop JSON operations (delta sync, plain-text detail, reply-draft flow)
#
# These return RAW Graph response dicts; the MCP tool layer owns the mapping
# into the structured shapes the desktop client consumes. Query strings are
# built by hand rather than via ``params=`` because Graph's OData parser
# rejects ``+`` as a space in $filter/$orderby — urlencode would emit ``+``.
# ---------------------------------------------------------------------------

# Exactly the fields the desktop list view needs. One constant so the fresh
# start and any future callers cannot drift apart.
DELTA_SELECT = (
    "id,internetMessageId,conversationId,subject,from,toRecipients,"
    "receivedDateTime,isRead,isDraft,bodyPreview"
)

DETAIL_SELECT = "id,uniqueBody,internetMessageHeaders,hasAttachments"

# Ask Exchange to convert the body server-side so the client never parses HTML.
_PREFER_TEXT_BODY = 'outlook.body-content-type="text"'


def _delta_path(folder: str, min_received: str) -> str:
    """Build the fresh-start delta URL for a folder."""
    path = f"/me/mailFolders/{_safe_id(folder)}/messages/delta?$select={quote(DELTA_SELECT)}"
    if min_received:
        path += f"&$filter={quote(f'receivedDateTime ge {min_received}')}"
    return path


def delta_page(
    client: GraphClient,
    folder: str = "inbox",
    cursor: str = "",
    min_received: str = "",
) -> dict[str, Any]:
    """Fetch ONE page of the folder delta feed.

    An empty cursor starts a fresh enumeration. A non-empty cursor is a Graph
    nextLink/deltaLink — an absolute URL that must be fetched verbatim.
    """
    if cursor:
        return client.get(cursor)
    return client.get(_delta_path(folder, min_received))


def get_message_detail(client: GraphClient, message_id: str) -> dict[str, Any]:
    """Fetch a message's plain-text body, internet headers, and attachment flag."""
    return client.get(
        f"/me/messages/{_safe_id(message_id)}?$select={quote(DETAIL_SELECT)}",
        headers={"Prefer": _PREFER_TEXT_BODY},
    )


def create_reply_draft(client: GraphClient, message_id: str, timezone: str = "") -> dict[str, Any]:
    """Create a reply draft for a message.

    A timezone is sent as a ``Prefer: outlook.timezone`` header so the quoted
    original carries local timestamps. Some mailboxes reject the header with a
    400 — retry once without it, because a draft with UTC-quoted timestamps
    beats no draft at all.
    """
    path = f"/me/messages/{_safe_id(message_id)}/createReply"
    if timezone:
        try:
            return client.post(path, headers={"Prefer": f'outlook.timezone="{timezone}"'}) or {}
        except GraphError as e:
            if e.status_code != 400:
                raise
            logger.debug("createReply rejected Prefer: outlook.timezone; retrying without it")
    return client.post(path) or {}


def update_draft_body(client: GraphClient, draft_id: str, text: str) -> dict[str, Any]:
    """Replace a draft's body with plain text."""
    return client.patch(
        f"/me/messages/{_safe_id(draft_id)}",
        {"body": {"contentType": "text", "content": text}},
    )


def send_draft(client: GraphClient, draft_id: str) -> None:
    """Send an existing draft. Graph answers 202 with no body."""
    client.post(f"/me/messages/{_safe_id(draft_id)}/send")


async def adelta_page(
    client: AsyncGraphClient,
    folder: str = "inbox",
    cursor: str = "",
    min_received: str = "",
) -> dict[str, Any]:
    """Fetch ONE page of the folder delta feed (async)."""
    if cursor:
        return await client.get(cursor)
    return await client.get(_delta_path(folder, min_received))


async def aget_message_detail(client: AsyncGraphClient, message_id: str) -> dict[str, Any]:
    """Fetch a message's plain-text body, internet headers, and attachment flag (async)."""
    return await client.get(
        f"/me/messages/{_safe_id(message_id)}?$select={quote(DETAIL_SELECT)}",
        headers={"Prefer": _PREFER_TEXT_BODY},
    )


async def acreate_reply_draft(
    client: AsyncGraphClient, message_id: str, timezone: str = ""
) -> dict[str, Any]:
    """Create a reply draft for a message (async).

    See :func:`create_reply_draft` for the Prefer-header retry rationale.
    """
    path = f"/me/messages/{_safe_id(message_id)}/createReply"
    if timezone:
        try:
            return (
                await client.post(path, headers={"Prefer": f'outlook.timezone="{timezone}"'})
            ) or {}
        except GraphError as e:
            if e.status_code != 400:
                raise
            logger.debug("createReply rejected Prefer: outlook.timezone; retrying without it")
    return (await client.post(path)) or {}


async def aupdate_draft_body(client: AsyncGraphClient, draft_id: str, text: str) -> dict[str, Any]:
    """Replace a draft's body with plain text (async)."""
    return await client.patch(
        f"/me/messages/{_safe_id(draft_id)}",
        {"body": {"contentType": "text", "content": text}},
    )


async def asend_draft(client: AsyncGraphClient, draft_id: str) -> None:
    """Send an existing draft (async). Graph answers 202 with no body."""
    await client.post(f"/me/messages/{_safe_id(draft_id)}/send")
