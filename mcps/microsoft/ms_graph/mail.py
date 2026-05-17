"""
Mail operations using the Microsoft Graph API.

All functions accept a GraphClient or AsyncGraphClient and return parsed dicts.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from .graph_client import GraphClient, AsyncGraphClient, GraphError

logger = logging.getLogger(__name__)

# Whitelist of standard HTML element names. Using a whitelist (rather than
# matching any word after <) prevents false positives on patterns like
# "Dear <FirstName>," or "if x<y and z>w" being mis-classified as HTML.
_HTML_TAG_NAMES = frozenset({
    'html', 'head', 'body', 'div', 'span', 'section', 'article',
    'header', 'footer', 'main', 'nav', 'aside', 'pre', 'blockquote',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'br', 'hr', 'wbr',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
    'a', 'strong', 'em', 'b', 'i', 'u', 's', 'del', 'ins', 'mark', 'small',
    'code', 'kbd', 'samp', 'sup', 'sub', 'abbr',
    'img', 'figure', 'figcaption', 'picture', 'source',
    'form', 'input', 'button', 'label', 'select', 'option', 'textarea',
    'script', 'style', 'link', 'meta', 'title',
    'font', 'center', 'nobr',
})
_HTML_TAG_RE = re.compile(
    r'</?(?:' + '|'.join(sorted(_HTML_TAG_NAMES, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)

_VALID_BODY_TYPES = frozenset({"HTML", "Text", "auto"})


def _detect_body_type(body: str) -> str:
    """Detect whether a body string is HTML or plain text.

    Searches for known HTML element names to classify the body. A whitelist
    avoids false positives on patterns like 'Dear <FirstName>,' or
    'if x<y and z>w' that contain angle brackets but are not HTML.
    """
    return "HTML" if _HTML_TAG_RE.search(body) else "Text"


def _extract_mailbox_address(odata_context: str) -> Optional[str]:
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

def get_profile(client: GraphClient) -> Dict[str, Any]:
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


async def aget_profile(client: AsyncGraphClient) -> Dict[str, Any]:
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
) -> List[Dict[str, Any]]:
    """List recent messages in a mail folder."""
    data = client.get(
        f"/me/mailFolders/{folder}/messages",
        params={"$top": top, "$orderby": "receivedDateTime desc"},
    )
    return data.get("value", [])


def get_message(client: GraphClient, message_id: str) -> Dict[str, Any]:
    """Get a single message by ID."""
    return client.get(f"/me/messages/{message_id}")


def send_message(
    client: GraphClient,
    to: List[str],
    subject: str,
    body: str,
    cc: Optional[List[str]] = None,
    from_address: Optional[str] = None,
    body_type: str = "auto",
) -> None:
    """Send an email message."""
    if body_type not in _VALID_BODY_TYPES:
        raise ValueError(f"body_type must be 'HTML', 'Text', or 'auto'; got {body_type!r}")
    to_recipients = [{"emailAddress": {"address": addr}} for addr in to]
    cc_recipients = [{"emailAddress": {"address": addr}} for addr in (cc or [])]
    effective_type = _detect_body_type(body) if body_type == "auto" else body_type

    payload: Dict[str, Any] = {
        "message": {
            "subject": subject,
            "body": {"contentType": effective_type, "content": body},
            "toRecipients": to_recipients,
        },
        "saveToSentItems": True,
    }
    if cc_recipients:
        payload["message"]["ccRecipients"] = cc_recipients
    if from_address:
        payload["message"]["from"] = {"emailAddress": {"address": from_address}}

    client.post("/me/sendMail", json_data=payload)


def search_messages(
    client: GraphClient,
    query: str,
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Search messages using KQL query syntax."""
    data = client.get(
        "/me/messages",
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
) -> List[Dict[str, Any]]:
    """List recent messages in a mail folder (async)."""
    data = await client.get(
        f"/me/mailFolders/{folder}/messages",
        params={"$top": top, "$orderby": "receivedDateTime desc"},
    )
    return data.get("value", [])


async def aget_message(client: AsyncGraphClient, message_id: str) -> Dict[str, Any]:
    """Get a single message by ID (async)."""
    return await client.get(f"/me/messages/{message_id}")


async def asend_message(
    client: AsyncGraphClient,
    to: List[str],
    subject: str,
    body: str,
    cc: Optional[List[str]] = None,
    from_address: Optional[str] = None,
    body_type: str = "auto",
) -> None:
    """Send an email message (async)."""
    if body_type not in _VALID_BODY_TYPES:
        raise ValueError(f"body_type must be 'HTML', 'Text', or 'auto'; got {body_type!r}")
    to_recipients = [{"emailAddress": {"address": addr}} for addr in to]
    cc_recipients = [{"emailAddress": {"address": addr}} for addr in (cc or [])]
    effective_type = _detect_body_type(body) if body_type == "auto" else body_type

    payload: Dict[str, Any] = {
        "message": {
            "subject": subject,
            "body": {"contentType": effective_type, "content": body},
            "toRecipients": to_recipients,
        },
        "saveToSentItems": True,
    }
    if cc_recipients:
        payload["message"]["ccRecipients"] = cc_recipients
    if from_address:
        payload["message"]["from"] = {"emailAddress": {"address": from_address}}

    await client.post("/me/sendMail", json_data=payload)


async def asearch_messages(
    client: AsyncGraphClient,
    query: str,
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Search messages using KQL query syntax (async)."""
    data = await client.get(
        "/me/messages",
        params={"$search": f'"{query}"', "$top": top},
    )
    return data.get("value", [])
