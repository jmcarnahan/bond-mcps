#!/usr/bin/env python3
"""
Microsoft Graph MCP Server.

Provides email, calendar, Teams, file, and Power BI tools. Tokens are resolved
in this order (see ms_graph/auth.py):
  1. Authorization: Bearer header (backend mode, e.g. Bond AI — Graph and Power BI
     get separate tokens via separate connection entries in bond_mcp_config)
  2. Local MSAL auth via ms_graph.local_auth (standalone mode — Claude Code, CLI),
     activated when MS_CLIENT_ID is set; reads/writes ~/.bond_mcps/microsoft.json

Run (standalone):
    make dev                                                                       # all 4 services
    poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 18001

Tool summary (48 tools):
  Email     : get_user_profile, list_emails, read_email, get_email_attachment, send_email,
              manage_inbox_rules, manage_mail_folders
  Calendar  : list_calendar_events, get_calendar_event, create_calendar_event, check_availability
  Teams     : list_teams, list_chats, read_teams_messages, get_teams_attachment,
              send_teams_message, get_teams_activity
  Files     : list_sharepoint_sites, list_files, inspect_file, upload_file, edit_document, manage_file
  Power BI  : list_powerbi_workspaces, list_powerbi_content, query_dataset, refresh_dataset, export_report
  Desktop JSON : get_profile_json, search_people_json, list_mail_delta, get_mail_detail,
                 get_mail_attachment_json, create_reply_draft_json, create_draft_json,
                 update_draft_body, add_draft_attachment_json, send_draft, mark_mail_read_json,
                 list_chats_page, get_chat_members_json, ensure_chat_json,
                 list_chat_messages_page, get_chat_attachment_json, mark_chat_read_json,
                 send_chat_message_json, inspect_file_json, connection_status

The 28 markdown tools above render prose for an LLM to read. The Desktop JSON
namespace is for programmatic clients (the desktop mail app) and follows a
different convention: every tool returns a ``dict``, which FastMCP surfaces as
structuredContent. Parameters stay ``str``/``int`` only (empty string = absent)
for Bedrock compatibility, as everywhere else in this server.
"""

import base64
import binascii
import html as html_mod
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import JSONResponse

load_dotenv(Path(__file__).parent / ".env")

from ms_graph import attachments as attachment_ops
from ms_graph import calendar as calendar_ops
from ms_graph import document_create, document_edit, mail_policy, workbook_edit
from ms_graph import files as files_ops
from ms_graph import folders as folder_ops
from ms_graph import mail as mail_ops
from ms_graph import people as people_ops
from ms_graph import power_bi as pbi_ops
from ms_graph import teams as teams_ops
from ms_graph.auth import get_graph_token, get_powerbi_token
from ms_graph.graph_client import AsyncGraphClient, GraphError, NonGraphUrlError
from ms_graph.local_auth import login_scopes
from ms_graph.people import DirectoryScopeMissingError
from ms_graph.power_bi import AsyncPowerBIClient
from ms_graph.teams import (
    FilesScopeMissingError,
    TeamsNotAvailableError,
    TeamsSearchUnsupportedError,
    extract_message_sender,
    extract_message_text,
)

from auth.connect_routes import (
    ProviderConnectConfig,
    register_connect_routes,
    register_status_routes,
)
from auth.jwt_identity import build_remote_auth_provider, register_noauth_wellknown
from auth.options_parser import opt_bool, opt_int, opt_str, parse_options


def _microsoft_post_exchange(token_response: dict) -> dict:
    """Microsoft returns access_token + refresh_token + expires_in in a
    standard OAuth response. The default shape works for us — we just
    normalize the fields TokenStore expects.

    The granted scopes arrive as ``scope`` and must be persisted under the
    storage key ``scopes`` — the key TokenRepository.save_token reads (see
    auth/auth/connect_routes.py ``_default_token_shape``). Rows written before
    this was corrected have NULL scopes; ``connection_status`` reports
    ``scopes: []`` for them, and clients treat empty as "unknown: assume mail
    granted, chat not".
    """
    import time

    out = {"access_token": token_response.get("access_token")}
    if rt := token_response.get("refresh_token"):
        out["refresh_token"] = rt
    if exp := token_response.get("expires_in"):
        try:
            out["expires_at"] = time.time() + int(exp)
        except (TypeError, ValueError):
            pass
    if scope := token_response.get("scope"):
        out["scopes"] = scope
    return out


# Microsoft OAuth flow uses the v2 endpoints. Tenant is configurable via
# MS_TENANT_ID; defaults to 'consumers' (personal MSA). The scope set
# matches what bond-ai's MCP config uses for Graph mail/calendar/files.
#
# Tenant is resolved LAZILY (at request time) so the env can be set after
# module import — important when the chart/container loads .env after the
# main module has already imported.
def _ms_tenant() -> str:
    return (os.environ.get("MS_TENANT_ID") or "consumers").strip() or "consumers"


def _ms_graph_scopes() -> str:
    """Graph scopes for the /connect flow: the login policy + offline_access.

    One policy for both consent doors (this flow and local MSAL), from
    ``ms_graph.local_auth.login_scopes``: the org default is exactly what the
    tenant admin has consented, because Entra treats the request as one
    bundle and a single admin-gated scope walls the WHOLE sign-in behind
    "Approval required" — mail included. Widening is config (``MS_SCOPES``),
    never code; a tool whose scope was not requested 403s at call time, which
    is the acceptable failure.

    ``offline_access`` is appended here and not in the login policy: MSAL
    manages refresh implicitly, while this code-grant flow must ask for the
    refresh token explicitly or the stored connection dies in an hour.
    """
    scopes = login_scopes()
    if "offline_access" not in scopes:
        scopes = [*scopes, "offline_access"]
    return " ".join(scopes)


MICROSOFT_CONNECT_CONFIG = ProviderConnectConfig(
    name="microsoft",
    authorize_url=lambda: f"https://login.microsoftonline.com/{_ms_tenant()}/oauth2/v2.0/authorize",
    token_url=lambda: f"https://login.microsoftonline.com/{_ms_tenant()}/oauth2/v2.0/token",
    # Callable, not a call: like the tenant above, the scope policy branches on
    # env (MS_TENANT_ID/MS_SCOPES) that may load after module import — an
    # import-time capture would hand an org tenant the consumer wish-list and
    # rebuild the exact "Approval required" wall login_scopes() removes.
    scopes=_ms_graph_scopes,
    client_id_env="MS_CLIENT_ID",
    client_secret_env="MS_CLIENT_SECRET",
    post_exchange=_microsoft_post_exchange,
)

POWERBI_CONNECT_CONFIG = ProviderConnectConfig(
    name="microsoft_powerbi",
    authorize_url=lambda: f"https://login.microsoftonline.com/{_ms_tenant()}/oauth2/v2.0/authorize",
    token_url=lambda: f"https://login.microsoftonline.com/{_ms_tenant()}/oauth2/v2.0/token",
    scopes="https://analysis.windows.net/powerbi/api/.default offline_access",
    client_id_env="MS_CLIENT_ID",
    client_secret_env="MS_CLIENT_SECRET",
    post_exchange=_microsoft_post_exchange,
)


logging.basicConfig(level=logging.INFO)
from auth import log_discipline  # noqa: E402

log_discipline.apply()
logger = logging.getLogger(__name__)


def _not_connected(e: PermissionError) -> dict:
    """Build the Desktop JSON "no Microsoft connection" payload.

    ``MissingProviderConnection`` (JWT mode) carries the per-user connect URL
    as an attribute; legacy laptop mode raises a plain PermissionError with no
    URL, which becomes None.
    """
    return {"error": "not_connected", "connect_url": getattr(e, "connect_url", None)}


@asynccontextmanager
async def _lifespan(app):
    """Fail fast on misconfig and warn if the auth proxy isn't reachable.

    verify_runtime_config() crashes the container at boot if BOND_MCPS_DB_URL
    is wrong, encryption key is missing, or BOND_MCPS_USER_ID is unset for
    Postgres — preventing the "container looks healthy to ECS but every
    request fails" pattern.
    """
    from auth import startup

    startup.verify_runtime_config()

    # A set-but-malformed allowlist stops the pod at boot rather than serving
    # mail unfiltered; the log line names the bad entry.
    domains = mail_policy.allowed_sender_domains()
    if domains:
        logger.info("Mail sender policy: on (%d allowed domain(s))", len(domains))
    else:
        logger.info("Mail sender policy: off (%s unset)", mail_policy.ENV_ALLOWED_SENDER_DOMAINS)

    if os.environ.get("MS_CLIENT_ID"):
        from auth import OAuthProxyClient

        proxy = OAuthProxyClient()
        try:
            proxy.check_proxy()
            logger.info("Auth proxy validated for local Microsoft auth")
        except RuntimeError as e:
            logger.warning("Auth proxy not available: %s", e)
    yield


mcp = FastMCP(
    "Microsoft Graph MCP Server", lifespan=_lifespan, auth=build_remote_auth_provider("ms-graph")
)

# Per-user provider OAuth bootstrap (JWT mode only).
register_connect_routes(mcp, MICROSOFT_CONNECT_CONFIG)
register_connect_routes(mcp, POWERBI_CONNECT_CONFIG)
register_status_routes(mcp, MICROSOFT_CONNECT_CONFIG)
register_status_routes(mcp, POWERBI_CONNECT_CONFIG)

# Return JSON (not HTML) for well-known probes in local mode so the MCP SDK
# doesn't log a noisy parse error.
register_noauth_wellknown(mcp)


# Liveness/readiness probe. Returns 200 immediately if the ASGI app is up.
# Does NOT touch the DB or auth proxy — those are validated at startup by
# `bond-mcps doctor`. Used by k8s probes + the ALB target-group healthcheck.
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    return JSONResponse({"status": "ok", "version": os.environ.get("BUILD_VERSION", "dev")})


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
async def list_emails(
    folder: str = "inbox", query: str = "", top: int = 1000, mailbox: str = "", options: str = ""
) -> str:
    """
    List recent emails or search email messages. Returns pipe-delimited CSV.

    When query is empty, lists recent messages in the specified folder (default: inbox).
    When query is provided, searches messages matching the keyword query. A custom
    folder is scoped to that folder; the default inbox searches across all folders.
    Custom folder display names (e.g. "solarwinds") are resolved to their folder ID
    automatically. Automatically paginates to fetch up to `top` messages.

    Args:
        folder: Mail folder to list from (default: inbox). Accepts well-known names
            (inbox, sentitems, drafts, ...) or a custom folder's display name.
            When query is set with a non-default folder, the search is scoped to it.
        query: Search query (e.g., "from:alice budget report"). Empty to list without searching.
        top: Maximum number of messages to return (default: 1000).
        mailbox: Shared mailbox email address (e.g. "support@company.com"). Leave empty
            to access your own mailbox. Requires Mail.Read.Shared permission and Exchange
            Full Access delegation on the shared mailbox.
        options: JSON string with optional fields:
            {"mark_as_read": ["id1", "id2"]}  — mark specified message IDs as read after listing.

    Returns:
        Pipe-delimited CSV. The has_attachments column flags messages that carry
        attachments; read_email lists them and get_email_attachment reads one.
        While the mail sender policy is on, messages from senders outside the
        allowed domains are omitted and a notice is appended.
    """
    import csv
    import io

    opts, err = parse_options(options)
    if err:
        return err

    _SELECT = (
        "id,subject,from,sender,isDraft,toRecipients,receivedDateTime,isRead,"
        "hasAttachments,bodyPreview"
    )

    # Resolved before any Graph call so a malformed allowlist fails the call
    # instead of returning unfiltered mail. The notice is constant: a hidden
    # count would turn a $search query into a content oracle.
    notice = f"\n{mail_policy.POLICY_NOTICE}" if mail_policy.enabled() else ""

    mb = mailbox or None
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        # A custom folder display name (anything other than the default inbox) is
        # resolved to a real folder ID; well-known names pass through unchanged.
        resolved_folder: str | None = None
        if folder and folder != "inbox":
            try:
                resolved_folder = await folder_ops.aresolve_folder_id(client, folder, mailbox=mb)
            except folder_ops.FolderNotFoundError as e:
                return str(e)

        if query:
            # Scope the search to an explicit custom folder; the default inbox
            # keeps the historical global-search behavior.
            messages = await mail_ops.asearch_messages(
                client, query=query, top=top, select=_SELECT, mailbox=mb, folder=resolved_folder
            )
        else:
            messages = await mail_ops.alist_messages(
                client, folder=resolved_folder or folder, top=top, select=_SELECT, mailbox=mb
            )
        messages = mail_policy.filter_messages(messages)

        mark_ids = opts.get("mark_as_read", [])
        if mark_ids:
            if not isinstance(mark_ids, list):
                return "Option 'mark_as_read' must be a JSON array of message IDs."
            for mid in mark_ids:
                await mail_ops.amark_read(client, mid, mailbox=mb)

    if not messages:
        prefix = f'No messages found matching "{query}".' if query else "No messages found."
        if mark_ids and isinstance(mark_ids, list):
            return f"{prefix}\n\n{len(mark_ids)} message(s) marked as read.{notice}"
        return f"{prefix}{notice}"

    output = io.StringIO()
    if query:
        output.write(f'{len(messages)} result(s) for "{query}"\n\n')
    else:
        output.write(f"{len(messages)} message(s) in {folder}\n\n")

    writer = csv.writer(output, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(
        [
            "date",
            "from_name",
            "from_address",
            "to",
            "subject",
            "is_read",
            "body_preview",
            "has_attachments",
            "id",
        ]
    )
    for msg in messages:
        sender = msg.get("from", {}).get("emailAddress", {})
        to_addrs = ", ".join(
            r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])
        )
        writer.writerow(
            [
                msg.get("receivedDateTime", ""),
                sender.get("name", ""),
                sender.get("address", ""),
                to_addrs,
                msg.get("subject", ""),
                msg.get("isRead", ""),
                msg.get("bodyPreview", ""),
                msg.get("hasAttachments", ""),
                msg.get("id", ""),
            ]
        )

    if mark_ids:
        output.write(f"\n{len(mark_ids)} message(s) marked as read.")

    return output.getvalue() + notice


def _attachment_line(summary: dict, show_inline: bool) -> str:
    """Render one attachment summary as a markdown bullet."""
    name = summary["name"] or "(unnamed)"
    suffix = " (inline)" if show_inline and summary["is_inline"] else ""
    if summary["kind"] == "reference":
        detail = f"link: {summary['source_url'] or '(no URL)'}"
    elif summary["kind"] == "item":
        detail = f"attached message, {_format_size(summary['size'])}"
    else:
        content_type = summary["content_type"] or "unknown type"
        detail = f"{content_type}, {_format_size(summary['size'])}"
    return f"- {name} — {detail}{suffix} — id: `{summary['id']}`"


def _format_attachment_section(attachments: list[dict], include_inline: bool) -> str:
    """Render the attachment block appended to a read email.

    Inline images (embedded signatures, logos) are noise in a body an LLM is
    reading, so they are counted and hidden unless explicitly asked for.
    """
    summaries = [attachment_ops.attachment_summary(att) for att in attachments]
    hidden = 0 if include_inline else sum(1 for s in summaries if s["is_inline"])
    shown = summaries if include_inline else [s for s in summaries if not s["is_inline"]]

    lines = [f"\n\n**Attachments ({len(shown)}):**"]
    lines.extend(_attachment_line(s, include_inline) for s in shown)
    if hidden:
        plural = "image" if hidden == 1 else "images"
        lines.append(
            f'(+{hidden} inline {plural} not shown; pass options {{"include_inline": true}})'
        )
    return "\n".join(lines)


@mcp.tool()
async def read_email(message_id: str, mailbox: str = "", options: str = "") -> str:
    """
    Read a single email message by its ID.

    When the message has attachments they are listed under the body with their
    names, types, sizes, and IDs. Pass an ID to get_email_attachment to read,
    download, or save one.

    Args:
        message_id: The Graph API message ID (from list_emails output).
        mailbox: Shared mailbox email address (e.g. "support@company.com"). Leave empty
            to access your own mailbox. Requires Mail.Read.Shared permission and Exchange
            Full Access delegation on the shared mailbox.
        options: JSON string with optional fields:
            {"mark_as_read": true/false}  — mark the email read (or unread) after reading.
            {"max_content_length": -1}  — max characters for the email body. Default -1
                (no limit). Set a positive integer to truncate long emails.
            {"include_inline": true}  — also list inline images (embedded logos and
                signatures). Default false: they are counted, not listed.

    While the mail sender policy is on, a message from a sender outside the
    allowed domains is refused instead of read.
    """
    opts, err = parse_options(options)
    if err:
        return err

    max_content_length = opt_int(opts.get("max_content_length"), -1)
    include_inline = opt_bool(opts.get("include_inline"), False)

    # Resolved before the fetch so a malformed allowlist fails the call with no
    # Graph traffic at all; the message itself can only be judged after it is
    # fetched, which is why the refusal below sits inside the client block.
    policy_on = mail_policy.enabled()

    mb = mailbox or None
    token = get_graph_token()
    attachments: list[dict] = []
    attachments_note = ""
    async with AsyncGraphClient(token) as client:
        msg = await mail_ops.aget_message(client, message_id, mailbox=mb)
        if policy_on and not mail_policy.message_allowed(msg):
            return mail_policy.EXTERNAL_SENDER_TEXT
        has_attachments = bool(msg.get("hasAttachments"))
        if has_attachments:
            try:
                attachments = await attachment_ops.alist_message_attachments(
                    client, message_id, mailbox=mb
                )
            except GraphError as e:
                attachments_note = f"*(could not list attachments: {e})*"
        mark = opts.get("mark_as_read")
        if mark is not None:
            await mail_ops.amark_read(client, message_id, opt_bool(mark, True), mailbox=mb)

    sender = msg.get("from", {}).get("emailAddress", {})
    to_addrs = ", ".join(
        r.get("emailAddress", {}).get("address", "?") for r in msg.get("toRecipients", [])
    )
    body = msg.get("body", {})
    content = body.get("content", "")
    if body.get("contentType") != "text":
        body_text = content if max_content_length <= 0 else content[:max_content_length]
        content = f"[HTML content, {len(content)} chars]\n{body_text}"
    elif max_content_length > 0:
        content = content[:max_content_length]

    result = (
        f"**Subject:** {msg.get('subject', '(no subject)')}\n"
        f"**From:** {sender.get('name', '?')} <{sender.get('address', '?')}>\n"
        f"**To:** {to_addrs}\n"
        f"**Date:** {msg.get('receivedDateTime', '?')}\n\n"
        f"{content}"
    )

    if attachments_note:
        result += f"\n\n{attachments_note}"
    elif has_attachments:
        result += _format_attachment_section(attachments, include_inline)

    if mark is not None:
        state = "read" if opt_bool(mark, True) else "unread"
        result += f"\n\n---\n*Marked as {state}.*"

    return result


_ATTACHMENT_TOO_LARGE_TEXT = (
    'This attachment is too large for text extraction (limit: 50 MB). Use mode "onedrive".'
)


def _attachment_header(summary: dict) -> str:
    """The identity block every get_email_attachment answer opens with."""
    header = (
        f"**Name:** {summary['name'] or '(unnamed)'}\n"
        f"**Type:** {summary['content_type'] or 'unknown'} ({_format_size(summary['size'])})\n"
        f"**ID:** `{summary['id']}`"
    )
    if summary["is_inline"]:
        header += f"\n**Inline:** yes (content id {summary['content_id'] or 'none'})"
    return header


def _format_attached_message(item: dict) -> str:
    """Render an item attachment's inner message as readable markdown."""
    sender = (item.get("from") or {}).get("emailAddress") or {}
    body = item.get("body") or {}
    if body.get("contentType") == "text":
        rendered = body.get("content", "")
    else:
        content = body.get("content", "")
        rendered = item.get("bodyPreview", "")
        rendered += f"\n[HTML body, {len(content)} chars — preview only]"
    return (
        "\n\n---\n**Attached message**\n"
        f"**Subject:** {item.get('subject', '(no subject)')}\n"
        f"**From:** {sender.get('name', '?')} <{sender.get('address', '?')}>\n"
        f"**Date:** {item.get('receivedDateTime', '?')}\n\n"
        f"{rendered}"
    )


def _render_attachment_result(header: str, result: dict, mode: str) -> str:
    """Turn one sink result into the markdown the tool returns."""
    if mode == "text":
        if result.get("text") is not None:
            body = f"{header}\n\n---\n{result['text']}"
            if result.get("truncated"):
                body += "\n\n*(content truncated)*"
            return body
        reason = result.get("reason")
        if reason == "binary":
            return (
                f"{header}\n\nThis is a binary file and cannot be displayed as text. "
                'Use mode "onedrive" to save it, or "base64" if it is under 1 MB.'
            )
        if reason == "too_large":
            return f"{header}\n\n{_ATTACHMENT_TOO_LARGE_TEXT}"
        return f"{header}\n\nCould not extract text from this document."
    if mode == "base64":
        if result.get("error") == "too_large":
            limit = _format_size(attachment_ops.MAX_BASE64_RETURN_BYTES)
            return (
                f"{header}\n\nToo large to return as base64 (limit {limit}). "
                'Use mode "onedrive" or "text".'
            )
        return f"{header}\n\n**Base64 ({result['size']} bytes):**\n{result['base64']}"
    return (
        f"{header}\n\n**Saved to OneDrive:** {result['web_url']}\n"
        f"**Item ID:** `{result['item_id']}`"
    )


@mcp.tool()
async def get_email_attachment(
    message_id: str, attachment_id: str, mode: str = "text", mailbox: str = "", options: str = ""
) -> str:
    """
    Read, download, or save one attachment from an email message.

    Get the IDs from read_email, which lists a message's attachments.

    Args:
        message_id: The Graph API message ID (from list_emails output).
        attachment_id: The attachment ID (from read_email's attachment list).
        mode: How to return the attachment.
            "text" (default) — extract the text: Word, PowerPoint, Excel, and PDF
                documents are parsed, plain-text files are decoded, and binaries
                (images, archives) say so instead.
            "base64" — the raw bytes, base64-encoded. Only for attachments under
                1 MB; anything larger is refused, so use "onedrive" for those.
            "onedrive" — save a copy to your OneDrive and return the link.
        mailbox: Shared mailbox email address (e.g. "support@company.com"). Leave empty
            to access your own mailbox. Requires Mail.Read.Shared permission and Exchange
            Full Access delegation on the shared mailbox.
        options: JSON string with optional fields:
            {"folder_path": "Attachments"}  — OneDrive folder for mode "onedrive"
                (default "Attachments"; created if missing).
            {"site_id": ""}  — save to this SharePoint site's drive instead of OneDrive.

    Notes:
        An attached email (item attachment) renders its subject, sender, date, and
        body in "text" mode, and downloads as a .eml file in the other modes. A link
        attachment (a file shared by reference) has no bytes to return: every mode
        gives back its URL, which inspect_file can then read.

        While the mail sender policy is on, an attachment is refused when the
        message carrying it, or an attached message inside it, came from a
        sender outside the allowed domains.
    """
    opts, err = parse_options(options)
    if err:
        return err

    mode = mode.strip().lower() or "text"
    if mode not in ("text", "base64", "onedrive"):
        return f"mode must be one of: text, base64, onedrive; got {mode!r}"

    mb = mailbox or None
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        # Judge the parent message before any of its attachment metadata or
        # bytes are read, against the same mailbox the reads will use.
        if not await mail_policy.acheck_message(client, message_id, mb):
            return mail_policy.EXTERNAL_SENDER_TEXT

        meta = await attachment_ops.aget_attachment_metadata(
            client, message_id, attachment_id, mailbox=mb
        )
        summary = attachment_ops.attachment_summary(meta)
        header = _attachment_header(summary)

        if summary["kind"] == "reference":
            return (
                f"{header}\n**Link:** {summary['source_url'] or '(no URL)'}\n\n"
                "This is a link attachment; open the URL (or use inspect_file with it) "
                "to read the file."
            )

        # An attached message is judged by the same rule as a message. The
        # expanded item is fetched at most once per call and reused below; an
        # attached event or contact has no sender and is therefore hidden.
        expanded: dict | None = None
        if summary["kind"] == "item" and mail_policy.enabled():
            expanded = await attachment_ops.aget_item_attachment(
                client, message_id, attachment_id, mailbox=mb
            )
            if not mail_policy.message_allowed(expanded.get("item") or {}):
                return mail_policy.EXTERNAL_SENDER_TEXT

        # Decide from the metadata size, so an oversized attachment is refused
        # before its bytes cross the wire. Attached messages are downloaded as
        # .eml in base64 mode, so the same ceiling applies to them.
        if mode == "base64" and summary["size"] > attachment_ops.MAX_BASE64_RETURN_BYTES:
            limit = _format_size(attachment_ops.MAX_BASE64_RETURN_BYTES)
            return (
                f"{header}\n\nToo large to return as base64 (limit {limit}). "
                'Use mode "onedrive" or "text".'
            )

        if summary["kind"] == "item":
            if mode == "text":
                if expanded is None:
                    expanded = await attachment_ops.aget_item_attachment(
                        client, message_id, attachment_id, mailbox=mb
                    )
                return header + _format_attached_message(expanded.get("item") or {})
            data, header_type = await attachment_ops.aget_attachment_bytes(
                client, message_id, attachment_id, mailbox=mb
            )
            name = summary["name"] or attachment_id
            att = attachment_ops.ResolvedAttachment(
                name=name if name.lower().endswith(".eml") else f"{name}.eml",
                data=data,
                content_type="message/rfc822",
            )
        else:
            if mode == "text" and summary["size"] > files_ops.MAX_DOCUMENT_DOWNLOAD_BYTES:
                return f"{header}\n\n{_ATTACHMENT_TOO_LARGE_TEXT}"
            data, header_type = await attachment_ops.aget_attachment_bytes(
                client, message_id, attachment_id, mailbox=mb
            )
            name = summary["name"] or attachment_id
            att = attachment_ops.ResolvedAttachment(
                name=name,
                data=data,
                content_type=(
                    summary["content_type"]
                    or header_type
                    or attachment_ops.guess_content_type(name)
                ),
            )

        result = await attachment_ops.adeliver_attachment(
            client,
            att,
            mode,
            folder_path=opt_str(opts.get("folder_path")) or "Attachments",
            site_id=opt_str(opts.get("site_id")) or "",
        )

    return _render_attachment_result(header, result, mode)


@mcp.tool()
async def send_email(to: str, subject: str, body: str, mailbox: str = "", options: str = "") -> str:
    """
    Send an email message.

    Args:
        to: Recipient email address (comma-separated for multiple). Supports
            individual mailboxes and distribution lists/groups (e.g. "DL_Team@company.com").
        subject: Email subject line.
        body: Email body content. HTML is auto-detected via MIME sniffing — bodies
            containing HTML tags (e.g. <strong>, <a href="...">, <br>, <p>) are
            sent as HTML automatically. Use body_type in options to override.
        mailbox: Shared mailbox email address to send FROM (e.g. "support@company.com").
            Leave empty to send from your own mailbox. Requires Mail.Send.Shared
            permission and Exchange Send As delegation on the shared mailbox.
        options: JSON string with optional fields:
            {"body_type": "auto|HTML|Text", "cc": "a@b.com,c@d.com",
             "bcc": "x@y.com,z@w.com", "from_address": "alias@company.com"}
            {"attachments": [...]}  — a JSON array of attachment specs. There is no
             file system to read from, so each spec says where the bytes come from.
             Use exactly one source key per spec:
                {"name": "notes.txt", "text": "hello"}
                    — literal text, saved under that name.
                {"name": "report.docx", "text": "# Title\\n\\nbody"}
                    — a .docx name converts the markdown into a Word document;
                      a .xlsx name converts CSV text into a spreadsheet.
                {"name": "img.png", "base64": "iVBOR..."}
                    — raw bytes you already hold, base64-encoded.
                {"drive_item_id": "01ABC...", "site_id": ""}
                    — a file already in OneDrive or SharePoint (from list_files);
                      site_id is optional and selects a SharePoint drive.
                {"url": "https://contoso-my.sharepoint.com/:w:/p/..."}
                    — a OneDrive/SharePoint sharing link.
                {"message_id": "AAMk...", "attachment_id": "AAMk..."}
                    — forward an attachment from another email (IDs from read_email).
             "name" is required for text and base64, optional elsewhere (it defaults
             to the source file's own name). Any spec may set "content_type" to
             override the type guessed from the name. While the mail sender
             policy is on, a {"message_id", "attachment_id"} spec is refused
             when that message came from a sender outside the allowed domains.
    """
    opts, err = parse_options(options)
    if err:
        return err
    body_type = opts.get("body_type", "auto")
    cc = opts.get("cc", "")
    bcc = opts.get("bcc", "")
    from_address = opts.get("from_address", "")

    mb = mailbox or None
    token = get_graph_token()
    to_list = [addr.strip() for addr in to.split(",") if addr.strip()]
    cc_list = [addr.strip() for addr in cc.split(",") if addr.strip()] if cc else None
    bcc_list = [addr.strip() for addr in bcc.split(",") if addr.strip()] if bcc else None

    resolved: list = []
    async with AsyncGraphClient(token) as client:
        specs = opts.get("attachments")
        if specs is not None:
            try:
                resolved = await attachment_ops.aresolve_attachment_sources(client, specs)
            except ValueError as e:
                return str(e)
        await mail_ops.asend_message(
            client,
            to=to_list,
            subject=subject,
            body=body,
            cc=cc_list,
            bcc=bcc_list,
            from_address=from_address or None,
            body_type=body_type,
            mailbox=mb,
            attachments=resolved or None,
        )

    cc_note = f" (CC: {cc})" if cc else ""
    bcc_note = f" (BCC: {len(bcc_list)} recipients)" if bcc_list else ""
    source = f" from {mailbox}" if mb else ""
    attach_note = ""
    if resolved:
        listed = ", ".join(f"{a.name} ({_format_size(len(a.data))})" for a in resolved)
        attach_note = f" with {len(resolved)} attachment(s): {listed}"
    return f"Email sent to {to}{source}{cc_note}{bcc_note}{attach_note}."


def _summarize_rule_keys(predicate: dict) -> str:
    """Summarize a rule's conditions/actions as a comma-joined list of their keys."""
    return ", ".join(predicate.keys()) if isinstance(predicate, dict) else ""


def _format_rule_detail(rule: dict) -> str:
    """Render one inbox rule as readable JSON for full visibility into conditions/actions."""
    import json

    return json.dumps(rule, indent=2, default=str)


def _format_folder_detail(folder: dict) -> str:
    """Render one mail folder as readable JSON (includes its ID for use in other calls)."""
    import json

    return json.dumps(folder, indent=2, default=str)


@mcp.tool()
async def manage_inbox_rules(action: str = "list", rule_id: str = "", options: str = "") -> str:
    """
    Manage Outlook inbox rules (messageRules): list | get | create | update | delete.

    Args:
        action: list (default) | get | create | update | delete.
        rule_id: rule ID — required for get, update, delete.
        options: JSON object. For create/update, the rule definition, e.g.
            {"displayName": "From partner", "sequence": 2, "isEnabled": true,
             "conditions": {"senderContains": ["adele"]},
             "actions": {"forwardTo": [...], "stopProcessingRules": true}}
            Create requires displayName, sequence, and actions.

    Returns:
        list: pipe-delimited CSV (id|displayName|sequence|isEnabled|conditions|actions).
        get: full JSON of the rule object (conditions, actions with values).
        create/update: confirmation message with rule ID and name. While the
            mail sender policy is on, a rule that forwards or redirects mail is
            refused, because it would re-deliver external mail as internal.
        delete: confirmation message.
    """
    import csv
    import io

    action = action.strip().lower()
    valid_actions = {"list", "get", "create", "update", "delete"}
    if action not in valid_actions:
        return f"Unknown action {action!r}. Use one of: {', '.join(sorted(valid_actions))}."

    if action in ("get", "update", "delete") and not rule_id:
        return f"A rule_id is required for action {action!r}."

    if action in ("create", "update"):
        opts, err = parse_options(options)
        if err:
            return err
        if not isinstance(opts, dict) or not opts:
            return (
                f"Action {action!r} requires a non-empty options JSON object (the rule definition)."
            )
        if action == "create":
            missing = {"displayName", "sequence", "actions"} - opts.keys()
            if missing:
                return f"Action 'create' requires: {', '.join(sorted(missing))}."

        # Refused before the token is even acquired: a forwarding rule
        # re-originates every external message as an internal one, which is a
        # durable, self-service bypass of the whole policy.
        if mail_policy.enabled() and mail_policy.rule_forwards(opts):
            return mail_policy.FORWARDING_RULE_TEXT

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if action == "list":
            rules = await mail_ops.alist_inbox_rules(client)
            if not rules:
                return "No inbox rules found."
            output = io.StringIO()
            output.write(f"{len(rules)} rule(s)\n\n")
            writer = csv.writer(output, delimiter="|", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["id", "displayName", "sequence", "isEnabled", "conditions", "actions"])
            for rule in rules:
                writer.writerow(
                    [
                        rule.get("id", ""),
                        rule.get("displayName", ""),
                        rule.get("sequence", ""),
                        rule.get("isEnabled", ""),
                        _summarize_rule_keys(rule.get("conditions", {})),
                        _summarize_rule_keys(rule.get("actions", {})),
                    ]
                )
            return output.getvalue()

        if action == "get":
            rule = await mail_ops.aget_inbox_rule(client, rule_id)
            return _format_rule_detail(rule)

        if action == "create":
            created = await mail_ops.acreate_inbox_rule(client, opts) or {}
            return f"Rule {created.get('id', '?')} ({created.get('displayName', '?')}) created."

        if action == "update":
            updated = await mail_ops.aupdate_inbox_rule(client, rule_id, opts)
            return f"Rule {updated.get('id', rule_id)} ({updated.get('displayName', '?')}) updated."

        # delete
        await mail_ops.adelete_inbox_rule(client, rule_id)
        return f"Rule {rule_id} deleted."


# ---------------------------------------------------------------------------
# Mail folders
# ---------------------------------------------------------------------------


@mcp.tool()
async def manage_mail_folders(action: str = "list", folder_id: str = "", options: str = "") -> str:
    """
    Manage Outlook mail folders: list | get | create | rename | move | delete.

    Args:
        action: list (default) | get | create | rename | move | delete.
        folder_id: folder ID or well-known name (inbox, sentitems, drafts,
            deleteditems, junkemail, archive). Required for get, rename, move, delete.
        options: JSON object.
            list: {"parent_id": "<folder-id>", "top": 100, "include_hidden": false}
                — parent_id lists that folder's child folders (omit for top-level).
            create: {"display_name": "Projects", "parent_id": "<optional-parent>"}.
            rename: {"display_name": "New name"}.
            move: {"destination_id": "<folder-id-or-well-known-name>"}.

    Returns:
        list: pipe-delimited CSV (id|displayName|childFolderCount|totalItemCount|unreadItemCount).
        get: full JSON of the folder (including its ID for use in other calls).
        create/rename/move: confirmation message with folder ID and name.
        delete: confirmation message.
    """
    import csv
    import io

    action = action.strip().lower()
    valid_actions = {"list", "get", "create", "rename", "move", "delete"}
    if action not in valid_actions:
        return f"Unknown action {action!r}. Use one of: {', '.join(sorted(valid_actions))}."

    if action in ("get", "rename", "move", "delete") and not folder_id:
        return f"A folder_id is required for action {action!r}."

    opts, err = parse_options(options)
    if err:
        return err

    if action in ("create", "rename"):
        display_name = str(opts.get("display_name", "")).strip()
        if not display_name:
            return f"Action {action!r} requires a non-empty 'display_name' in options."

    if action == "move":
        destination_id = str(opts.get("destination_id", "")).strip()
        if not destination_id:
            return "Action 'move' requires a non-empty 'destination_id' in options."

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if action == "list":
            parent_id = opt_str(opts.get("parent_id"))
            top = max(1, opt_int(opts.get("top"), 100))
            result = await folder_ops.alist_folders(
                client,
                parent_id=parent_id,
                top=top,
                include_hidden=opt_bool(opts.get("include_hidden"), False),
            )
            if not result:
                return "No folders found."
            output = io.StringIO()
            output.write(f"{len(result)} folder(s)\n\n")
            writer = csv.writer(output, delimiter="|", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(
                ["id", "displayName", "childFolderCount", "totalItemCount", "unreadItemCount"]
            )
            for folder in result:
                writer.writerow(
                    [
                        folder.get("id", ""),
                        folder.get("displayName", ""),
                        folder.get("childFolderCount", ""),
                        folder.get("totalItemCount", ""),
                        folder.get("unreadItemCount", ""),
                    ]
                )
            return output.getvalue()

        if action == "get":
            folder = await folder_ops.aget_folder(client, folder_id)
            return _format_folder_detail(folder)

        if action == "create":
            parent_id = opt_str(opts.get("parent_id"))
            created = await folder_ops.acreate_folder(client, display_name, parent_id=parent_id)
            created = created or {}
            return f"Folder {created.get('id', '?')} ({created.get('displayName', '?')}) created."

        if action == "rename":
            updated = (await folder_ops.arename_folder(client, folder_id, display_name)) or {}
            return f"Folder {updated.get('id', folder_id)} renamed to {updated.get('displayName', '?')!r}."

        if action == "move":
            moved = (await folder_ops.amove_folder(client, folder_id, destination_id)) or {}
            return f"Folder {folder_id} moved to {moved.get('parentFolderId', destination_id)!r}."

        # delete
        await folder_ops.adelete_folder(client, folder_id)
        return f"Folder {folder_id} deleted."


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
    from datetime import datetime, timedelta
    from datetime import timezone as tz

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

        entry = f"{i}. **{subject}**{status}\n   Time: {time_str}\n"
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
async def get_calendar_event(event_id: str, options: str = "") -> str:
    """
    Get detailed information about a specific calendar event.

    Args:
        event_id: The event ID (from list_calendar_events output).
        options: JSON string with optional fields:
            {"max_content_length": -1}  — max characters for the event body. Default -1
                (no limit). Set a positive integer to truncate long event descriptions.
    """
    opts, err = parse_options(options)
    if err:
        return err

    max_content_length = opt_int(opts.get("max_content_length"), -1)

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
        body_text = body_content if max_content_length <= 0 else body_content[:max_content_length]
        body_content = f"[HTML content, {len(body_content)} chars]\n{body_text}"
    elif max_content_length > 0:
        body_content = body_content[:max_content_length]

    attendees = event.get("attendees", [])
    attendee_lines = []
    for att in attendees:
        email = att.get("emailAddress", {})
        status = att.get("status", {}).get("response", "none")
        attendee_lines.append(
            f"  - {email.get('name', '?')} <{email.get('address', '?')}> ({status})"
        )

    is_all_day = event.get("isAllDay", False)
    online_url = event.get("onlineMeetingUrl", "")
    recurrence = event.get("recurrence")

    lines = [
        f"**Subject:** {subject}",
        f"**Time:** {'All day' if is_all_day else f'{start_str} to {end_str}'}",
    ]
    if organizer:
        lines.append(
            f"**Organizer:** {organizer.get('name', '?')} <{organizer.get('address', '?')}>"
        )
    if location:
        lines.append(f"**Location:** {location}")
    if online_url:
        lines.append(f"**Online Meeting:** {online_url}")
    if recurrence:
        pattern = recurrence.get("pattern", {})
        lines.append(
            f"**Recurrence:** {pattern.get('type', 'unknown')} (every {pattern.get('interval', 1)} {pattern.get('type', '')})"
        )
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
    options: str = "",
) -> str:
    """
    Create a new calendar event.

    Args:
        subject: Event title/subject.
        start_datetime: Start date and time in ISO 8601 format (e.g., "2026-05-08T10:00:00").
        end_datetime: End date and time in ISO 8601 format (e.g., "2026-05-08T11:00:00").
        timezone: IANA timezone for start/end times (e.g., "America/New_York", "UTC"). Default: UTC.
        options: JSON string with optional fields:
            {"attendees": "a@b.com,c@d.com", "location": "Room 42", "body": "Meeting notes...", "is_online_meeting": true, "is_all_day": false}
    """
    opts, err = parse_options(options)
    if err:
        return err
    attendees = opts.get("attendees", "")
    location = opts.get("location", "")
    body = opts.get("body", "")
    is_online_meeting = opt_bool(opts.get("is_online_meeting"), False)
    is_all_day = opt_bool(opts.get("is_all_day"), False)

    attendee_list = (
        [addr.strip() for addr in attendees.split(",") if addr.strip()] if attendees else None
    )

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


def _is_chat_unread(chat: dict) -> bool:
    preview = chat.get("lastMessagePreview")
    if not preview:
        return False
    viewpoint = chat.get("viewpoint") or {}
    last_read = viewpoint.get("lastMessageReadDateTime")
    if not last_read:
        return True
    last_msg_date = preview.get("createdDateTime", "")
    if not last_msg_date:
        return False
    from datetime import datetime

    try:
        msg_dt = datetime.fromisoformat(last_msg_date.replace("Z", "+00:00"))
        read_dt = datetime.fromisoformat(last_read.replace("Z", "+00:00"))
        return msg_dt > read_dt
    except (ValueError, TypeError):
        return True


@mcp.tool()
async def list_chats(chat_type: str = "", top: int = 50, options: str = "") -> str:
    """
    List Teams chats (1:1, group, meeting) with last message preview.

    Args:
        chat_type: Filter by type: oneOnOne, group, or meeting. Empty for all.
        top: Maximum number of chats to return (default: 50, max: 2000).
        options: JSON string with optional fields:
            {"mark_as_read": ["chat_id1", "chat_id2"]}  — mark specified chat IDs as read after listing.
    """
    import csv
    import io

    opts, err = parse_options(options)
    if err:
        return err

    valid_types = {"", "oneOnOne", "group", "meeting"}
    if chat_type not in valid_types:
        return f"Invalid chat_type: {chat_type}. Must be one of: oneOnOne, group, meeting (or empty for all)."

    top = min(top, 2000)

    mark_ids = opts.get("mark_as_read", [])
    if mark_ids and not isinstance(mark_ids, list):
        return "Option 'mark_as_read' must be a JSON array of chat IDs."

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            chats = await teams_ops.alist_chats(client, chat_type=chat_type, top=top)

            if mark_ids:
                claims = teams_ops.decode_token_claims(token)
                if not claims["oid"] or not claims["tid"]:
                    return "Could not determine user identity for marking chats as read."
                for cid in mark_ids:
                    await teams_ops.amark_chat_read(client, cid, claims["oid"], claims["tid"])
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."

    if not chats:
        prefix = "No chats found."
        if mark_ids and isinstance(mark_ids, list):
            return f"{prefix}\n\n{len(mark_ids)} chat(s) marked as read."
        return prefix

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(
        ["unread", "type", "name", "members", "last_sender", "last_preview", "last_date", "id"]
    )
    for chat in chats:
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
        unread = _is_chat_unread(chat)
        writer.writerow(
            [
                unread,
                ct,
                label,
                members_str or "(unknown)",
                preview_sender,
                preview_text[:100],
                preview_date,
                chat.get("id", "?"),
            ]
        )

    output = f"{len(chats)} chat(s)\n{buf.getvalue()}"
    if mark_ids and isinstance(mark_ids, list):
        output += f"\n{len(mark_ids)} chat(s) marked as read."
    return output


_THUMBNAIL_WORDS = ("small", "medium", "large")


def _teams_attachment_column(entries: list[dict]) -> str:
    """The attachments column read_teams_messages writes, one row's worth.

    Files read ``name [file:<id>]``, inline images ``[image:<id>]``, cards
    ``[card]``. Quoted-message references and unknown kinds carry nothing to
    fetch, so they are left out rather than advertised as ids.
    """
    parts: list[str] = []
    for entry in entries:
        kind = entry["kind"]
        if kind == "file" and entry["id"]:
            name = entry["name"]
            parts.append(f"{name} [file:{entry['id']}]" if name else f"[file:{entry['id']}]")
        elif kind == "image" and entry["id"]:
            parts.append(f"[image:{entry['id']}]")
        elif kind == "card":
            parts.append("[card]")
    return "; ".join(parts)


def _hosted_image_name(hosted_content_id: str, content_type: str) -> str:
    """Inline images carry no name; derive one from the id and the MIME type."""
    ext = mimetypes.guess_extension(content_type) or ".bin"
    return f"image-{hosted_content_id[:12]}{ext}"


def _mime_from_header(header: str, fallback: str) -> str:
    """'image/png; charset=utf-8' -> 'image/png'; an empty header -> fallback."""
    mime = (header or "").split(";")[0].strip()
    return mime or fallback


def _teams_entry_summary(
    entry: dict, name: str | None, content_type: str | None, size: int
) -> dict:
    """The summary shape _attachment_header renders, built from a Teams entry.

    Teams has no inline/content-id concept for shared files, so those two keys
    are constant here; they exist because mail attachments carry them.
    """
    return {
        "id": entry["id"],
        "name": name,
        "content_type": content_type,
        "size": size,
        "is_inline": False,
        "content_id": None,
    }


async def _find_teams_attachment(
    client,
    message_id: str,
    attachment_id: str,
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
) -> tuple[dict | None, list[dict]]:
    """Fetch one message and locate an attachment on it by id.

    Returns (entry_or_None, all_entries) — the full list so a caller can tell
    the user what ids the message actually has.
    """
    msg = await teams_ops.aget_message(
        client, message_id, chat_id=chat_id, team_id=team_id, channel_id=channel_id
    )
    entries = teams_ops.parse_message_attachments(msg)
    for entry in entries:
        if entry["id"] == attachment_id:
            return entry, entries
    return None, entries


@mcp.tool()
async def read_teams_messages(
    team_id: str = "",
    channel_id: str = "",
    chat_id: str = "",
    since: str = "",
    options: str = "",
) -> str:
    """
    Read messages from a Teams channel or chat back to a given date.

    Paginates internally to fetch all messages since the cutoff date.
    Default: messages from the last 7 days.

    Provide either:
    - chat_id to read from a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to read from a team channel (from list_teams)

    Args:
        team_id: Team ID (from list_teams with no team_id). Required for channel reading.
        channel_id: Channel ID (from list_teams with team_id). Required for channel reading.
        chat_id: Chat ID (from list_chats). Use this for 1:1 and group chats.
        since: ISO date or datetime cutoff (e.g. '2026-06-16' or '2026-06-16T00:00:00Z').
               Messages older than this are excluded. Default: 7 days ago.
        options: JSON string with optional fields:
            {"mark_as_read": true/false}  — mark the chat as read after reading messages.
                Only works with chat_id (channels don't support per-user read state).
            {"max_content_length": -1}  — max characters per message body. Default -1
                (no limit). Set a positive integer to truncate long messages.

    Each row's attachments column lists shared files as `name [file:<id>]`, inline
    images as `[image:<id>]`, and cards as `[card]`; pass a file or image id to
    get_teams_attachment to read or download it. The content column also carries
    `[File: name]` / `[Image]` markers.
    """
    import csv
    import io
    import re
    from datetime import datetime, timedelta, timezone

    opts, err = parse_options(options)
    if err:
        return err

    if not chat_id and not (team_id and channel_id):
        return "Provide either chat_id, or both team_id and channel_id."

    if since:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", since):
            since += "T00:00:00Z"
        elif not re.match(r"^\d{4}-\d{2}-\d{2}T", since):
            return f"Invalid since format: '{since}'. Use YYYY-MM-DD or ISO datetime."
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    max_content_length = opt_int(opts.get("max_content_length"), -1)
    mark = opts.get("mark_as_read")
    should_mark = mark is not None and opt_bool(mark, True)
    if mark is not None and not chat_id:
        return "Option 'mark_as_read' is only supported for chats, not channels."

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            if chat_id:
                messages = await teams_ops.alist_chat_messages(client, chat_id, since=since)
                source = f"chat `{chat_id}`"
            else:
                messages = await teams_ops.alist_channel_messages(
                    client, team_id, channel_id, since=since
                )
                source = f"channel `{channel_id}`"

            if should_mark:
                claims = teams_ops.decode_token_claims(token)
                if not claims["oid"] or not claims["tid"]:
                    return "Could not determine user identity for marking chat as read."
                await teams_ops.amark_chat_read(client, chat_id, claims["oid"], claims["tid"])
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."

    if not messages:
        return f"No messages found in {source} since {since}."

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["timestamp", "sender", "content", "attachments", "id"])
    for msg in messages:
        sender = extract_message_sender(msg)
        content = extract_message_text(msg, max_length=max_content_length)
        writer.writerow(
            [
                msg.get("createdDateTime", ""),
                sender,
                content or "(empty)",
                _teams_attachment_column(teams_ops.parse_message_attachments(msg)),
                msg.get("id", ""),
            ]
        )

    result = f"{len(messages)} message(s) in {source} since {since}\n{buf.getvalue()}"
    if should_mark:
        result += "\n---\n*Chat marked as read.*"
    return result


@mcp.tool()
async def search_teams_messages(
    query: str,
    since: str = "",
    conversation_id: str = "",
    options: str = "",
) -> str:
    """
    Search Teams messages across every chat and channel by hashtag or keyword.

    Covers 1:1 chats, group chats, and team channels in one search. Use this
    when you do not know which conversation a message is in; use
    read_teams_messages when you already have a chat_id or channel_id.

    Hashtags: a '#tag' in the query is matched exactly. Microsoft's index
    ignores the '#', so a bare search for '#budget2026' would also return
    messages that merely say 'budget2026'; this tool re-reads each message and
    keeps only the ones that literally contain '#budget2026'. Several hashtags mean AND —
    '#budget2026 #q3' returns only messages carrying both.

    Plain keywords ('invoice approved') are passed to Microsoft Search as
    typed, with stemming, and are NOT re-checked. Keyword-Query-Language terms
    a user types are passed through too, e.g. 'from:todd' or 'sent>=2026-01-01'.

    Args:
        query: Hashtags ('#budget2026'), keywords ('invoice approved'), or both.
        since: ISO date or datetime cutoff, e.g. '2026-01-01' or
               '2026-01-01T00:00:00Z'. EMPTY MEANS ALL TIME — unlike
               read_teams_messages, which defaults to the last 7 days.
        conversation_id: Restrict to one conversation. Accepts a chat id from
               list_chats or a channel id from list_teams. Empty searches
               everywhere.
        options: JSON string with optional fields:
            {"max_results": 25}  — messages to return (default 25, max 100).
            {"exact": true}  — force literal hashtag checking on or off.
                Default: on when the query contains a '#tag', off otherwise.
                Turning it off returns everything the index matched, stemming
                and all.
            {"max_content_length": -1}  — max characters per message body.
                Default -1 (no limit).

    Returns a header line and a '|'-delimited table with columns:
    timestamp, sender, conversation, content, attachments, id, link.
    The conversation column reads 'chat:<chat_id>' or
    'channel:<team_id>/<channel_id>' — pass those ids to read_teams_messages to
    read the surrounding thread, or the id column to get_teams_attachment.
    The link column is the message's Teams deep link.

    Searching is only available on work or school accounts.
    """
    import csv
    import io

    opts, err = parse_options(options)
    if err:
        return err

    if not query or not query.strip():
        return "Provide a search query."

    try:
        since = teams_ops.normalize_since(since)
    except ValueError as e:
        return str(e)

    max_results = opt_int(opts.get("max_results"), teams_ops.SEARCH_DEFAULT_MAX_RESULTS)
    max_content_length = opt_int(opts.get("max_content_length"), -1)
    exact_opt = opts.get("exact")
    exact = None if exact_opt is None else opt_bool(exact_opt, True)

    scope_id = conversation_id.strip()
    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            found = await teams_ops.asearch_messages(
                client,
                query,
                since=since,
                conversation_id=scope_id,
                max_results=max_results,
                exact=exact,
            )
    except TeamsSearchUnsupportedError:
        return (
            "**Teams message search is not available for this account.**\n"
            "Microsoft Search covers work and school accounts only. Read a "
            "specific conversation with read_teams_messages instead."
        )
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."

    scope = f" in `{scope_id}`" if scope_id else ""
    window = f" since {since}" if since else " (all time)"
    messages = found["messages"]
    if not messages:
        note = ""
        hydrated = found["candidates"] - found["skipped"]
        if found["exact"] and found["hashtags"] and hydrated > 0:
            note = (
                f" The index matched {hydrated} message(s) but none carried "
                'the hashtag literally; retry with {"exact": false} to see them.'
            )
        if found["skipped"]:
            note += (
                f" {found['skipped']} matching message(s) could not be read "
                "(deleted, or no longer shared with you) and were skipped."
            )
        return f"No messages found matching `{query}`{scope}{window}.{note}"

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["timestamp", "sender", "conversation", "content", "attachments", "id", "link"])
    for msg in messages:
        writer.writerow(
            [
                msg.get("createdDateTime", ""),
                extract_message_sender(msg),
                msg.get("_conversation", ""),
                extract_message_text(msg, max_length=max_content_length) or "(empty)",
                _teams_attachment_column(teams_ops.parse_message_attachments(msg)),
                msg.get("id", ""),
                msg.get("_web_link", ""),
            ]
        )

    result = f"{len(messages)} message(s) matching `{query}`{scope}{window}\n{buf.getvalue()}"
    if found["skipped"]:
        result += (
            f"\n*{found['skipped']} matching message(s) could not be read "
            "(deleted, or no longer shared with you) and were skipped.*"
        )
    if found["truncated"]:
        result += (
            "\n*More results may exist. Narrow the search with since or "
            "conversation_id, or raise max_results.*"
        )
    return result


@mcp.tool()
async def get_teams_attachment(
    message_id: str,
    attachment_id: str,
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
    mode: str = "text",
    options: str = "",
) -> str:
    """
    Read, download, or save one file or inline image from a Teams message.

    The ids come from read_teams_messages' attachments column: a shared file
    shows as `name [file:<id>]` and an inline image as `[image:<id>]`. Pass the
    id inside the brackets.

    Provide either:
    - chat_id to read from a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to read from a team channel (from list_teams)

    Args:
        message_id: The message ID (the id column of read_teams_messages).
        attachment_id: The file or image ID from that row's attachments column.
        chat_id: Chat ID (from list_chats). Takes priority over team/channel.
        team_id: Team ID (from list_teams with no team_id).
        channel_id: Channel ID (from list_teams with team_id).
        mode: How to return the attachment.
            "text" (default) — extract the text: Word, PowerPoint, Excel, and PDF
                documents are parsed, plain-text files are decoded, and binaries
                (images, archives) report their type and size instead.
            "base64" — the raw bytes, base64-encoded. Only for attachments under
                1 MB; anything larger is refused, so use "onedrive" for those.
            "onedrive" — save a copy to your OneDrive and return the link.
        options: JSON string with optional fields:
            {"folder_path": "Attachments"}  — OneDrive folder for mode "onedrive"
                (default "Attachments"; created if missing).
            {"site_id": ""}  — save to this SharePoint site's drive instead of OneDrive.

    Notes:
        A card attachment renders its text here rather than downloading. A quoted
        message reference has nothing to download. A file shared in Teams that
        your account cannot open reports access denied — the owner has to
        re-share it with you.
    """
    opts, err = parse_options(options)
    if err:
        return err

    mode = mode.strip().lower() or "text"
    if mode not in ("text", "base64", "onedrive"):
        return f"mode must be one of: text, base64, onedrive; got {mode!r}"

    if not chat_id and not (team_id and channel_id):
        return "Provide either chat_id, or both team_id and channel_id."
    if chat_id:
        # A chat wins over a team/channel pair, matching read_teams_messages —
        # the ops layer refuses both at once, and that is not the caller's bug.
        team_id = channel_id = ""

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            entry, entries = await _find_teams_attachment(
                client,
                message_id,
                attachment_id,
                chat_id=chat_id,
                team_id=team_id,
                channel_id=channel_id,
            )
            if entry is None:
                available = (
                    "; ".join(f"{e['kind']}: {e['id']}" for e in entries if e["id"]) or "(none)"
                )
                return (
                    f"No attachment with id `{attachment_id}` on message `{message_id}`.\n"
                    f"Available: {available}"
                )

            kind = entry["kind"]
            if kind == "card":
                card_text = entry["card_text"] or "(no readable text)"
                return f"**Card:** {entry['content_type']}\n\n{card_text}"
            if kind == "message_reference":
                return (
                    "This attachment is a quoted message reference, not a file; "
                    "there is nothing to download."
                )
            if kind == "other":
                answer = (
                    f"Attachment of type {entry['content_type'] or 'unknown'} "
                    "cannot be fetched by this tool."
                )
                if entry["content_url"]:
                    answer += f"\nURL: {entry['content_url']}"
                return answer

            if kind == "file":
                url = entry["content_url"]
                if not url:
                    return "This file attachment has no content URL to resolve."
                try:
                    item = await files_ops.aresolve_sharing_link(client, url)
                except GraphError as e:
                    if e.status_code == 403:
                        return (
                            "**Access denied:** the file was shared in Teams but your "
                            "account cannot open it. The owner may need to re-share it "
                            "with you."
                        )
                    if e.status_code == 404:
                        return (
                            "**Item not found** for this file's link. The link may have "
                            "expired, been revoked, or the file was deleted."
                        )
                    if e.status_code == 400:
                        return "**Invalid sharing link.** Could not resolve this file's URL."
                    raise

                if "folder" in item:
                    return "This attachment is a folder, not a file. Use list_files to browse it."

                name = entry["name"] or item.get("name") or attachment_id
                content_type = (item.get("file") or {}).get(
                    "mimeType"
                ) or attachment_ops.guess_content_type(name)
                raw_size = item.get("size")
                size = raw_size if isinstance(raw_size, int) else 0
                summary = _teams_entry_summary(entry, name, content_type, size)
                header = _attachment_header(summary)

                # Decided from the driveItem metadata, so an oversized file is
                # refused before its bytes ever cross the wire.
                if mode == "text" and size > files_ops.MAX_DOCUMENT_DOWNLOAD_BYTES:
                    return f"{header}\n\n{_ATTACHMENT_TOO_LARGE_TEXT}"
                if mode == "base64" and size > attachment_ops.MAX_BASE64_RETURN_BYTES:
                    limit = _format_size(attachment_ops.MAX_BASE64_RETURN_BYTES)
                    return (
                        f"{header}\n\nToo large to return as base64 (limit {limit}). "
                        'Use mode "onedrive" or "text".'
                    )

                try:
                    _, data = await files_ops.aresolve_sharing_link_bytes(client, url, item=item)
                except ValueError as e:
                    return f"{header}\n\n{e}"
                att = attachment_ops.ResolvedAttachment(
                    name=name, data=data, content_type=content_type
                )
            else:
                # hostedContents advertises no size before $value, so there is
                # nothing to pre-check; the sink refuses oversize base64 itself.
                data, header_type = await teams_ops.aget_hosted_content(
                    client,
                    message_id,
                    entry["id"],
                    chat_id=chat_id,
                    team_id=team_id,
                    channel_id=channel_id,
                )
                content_type = _mime_from_header(header_type, "application/octet-stream")
                name = _hosted_image_name(entry["id"], content_type)
                summary = _teams_entry_summary(entry, name, content_type, len(data))
                header = _attachment_header(summary)
                att = attachment_ops.ResolvedAttachment(
                    name=name, data=data, content_type=content_type
                )

            result = await attachment_ops.adeliver_attachment(
                client,
                att,
                mode,
                folder_path=opt_str(opts.get("folder_path")) or "Attachments",
                site_id=opt_str(opts.get("site_id")) or "",
            )
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."

    return _render_attachment_result(header, result, mode)


def _teams_send_summary(
    to_chat: bool,
    sent_files: list,
    sent_images: list,
) -> str:
    """Confirm what actually went out, naming each file so a wrong one is obvious."""
    summary = "Message sent to Teams chat" if to_chat else "Message sent to Teams channel"
    if sent_files:
        listed = ", ".join(f"{f.name} ({_format_size(len(f.data))})" for f in sent_files)
        summary += f" with {len(sent_files)} file(s): {listed}"
        if sent_images:
            summary += f" and {len(sent_images)} inline image(s)"
    elif sent_images:
        summary += f" with {len(sent_images)} inline image(s)"
    return summary + "."


@mcp.tool()
async def send_teams_message(
    message: str,
    team_id: str = "",
    channel_id: str = "",
    chat_id: str = "",
    options: str = "",
) -> str:
    """
    Send a message to a Teams channel or chat, with optional @mentions.

    Provide either:
    - chat_id to send to a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to send to a team channel (from list_teams)

    Args:
        message: Message content to send. Supports plain text (newlines preserved)
            or HTML (e.g. '<a href="https://example.com">Click here</a>').
        team_id: Team ID (from list_teams with no team_id). Required for channel sending.
        channel_id: Channel ID (from list_teams with team_id). Required for channel sending.
        chat_id: Chat ID (from list_chats). Use this for 1:1 and group chats.
        options: JSON string with optional fields:
            {"content_type": "auto|html|text",
             "mentions": [{"user_id": "aad-object-id", "name": "Display Name"}],
             "mention_everyone": true,
             "attachments": [{"name": "notes.txt", "text": "..."}],
             "images": [{"name": "chart.png", "base64": "..."}]}
            User IDs (AAD object IDs) can be found in list_teams or list_chats member lists.

            attachments take the same source specs as send_email — {"name", "text"},
            {"name", "base64"}, {"drive_item_id"}, {"url"} (a sharing link), or
            {"message_id", "attachment_id"}. Teams cannot carry file bytes on a
            message, so each file is uploaded to OneDrive first (a chat: the
            "Microsoft Teams Chat Files" folder, shared read-only with that chat's
            members; a channel: the channel's Files folder) and the message posts a
            card pointing at it. That upload needs the Files.ReadWrite permission.

            images use the same specs but must be image/* under 4 MB; they render
            inline in the message body instead of appearing as files.
    """
    if not chat_id and not (team_id and channel_id):
        return "Provide either chat_id, or both team_id and channel_id."

    opts, err = parse_options(options)
    if err:
        return err
    content_type = opts.get("content_type", "auto")
    raw_mentions = opts.get("mentions", [])
    mention_everyone = opts.get("mention_everyone", False)

    graph_mentions: list[dict] = []
    mention_id = 0

    if mention_everyone and team_id and channel_id:
        graph_mentions.append(
            {
                "id": mention_id,
                "mentionText": "Everyone",
                "mentioned": {
                    "conversation": {
                        "id": channel_id,
                        "displayName": "Everyone",
                        "conversationIdentityType": "channel",
                    }
                },
            }
        )
        mention_id += 1

    if isinstance(raw_mentions, list):
        for m in raw_mentions:
            if not isinstance(m, dict):
                continue
            user_id = m.get("user_id", "")
            name = m.get("name", "")
            if not user_id or not name:
                continue
            graph_mentions.append(
                {
                    "id": mention_id,
                    "mentionText": name,
                    "mentioned": {
                        "user": {
                            "id": user_id,
                            "displayName": name,
                            "userIdentityType": "aadUser",
                        }
                    },
                }
            )
            mention_id += 1

    if graph_mentions:
        content_type = "html"
        message = html_mod.escape(message, quote=False)
        at_tags: list[str] = []
        for gm in graph_mentions:
            mid = gm["id"]
            at_tags.append(f'<at id="{mid}">{html_mod.escape(gm["mentionText"], quote=False)}</at>')
        message = " ".join(at_tags) + " " + message

    mentions_payload = graph_mentions if graph_mentions else None

    everyone_ignored = mention_everyone and chat_id and not (team_id and channel_id)
    everyone_note = (
        " (Note: mention_everyone only works in channels, ignored here.)"
        if everyone_ignored
        else ""
    )

    file_specs = opts.get("attachments")
    image_specs = opts.get("images")

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            if file_specs is not None or image_specs is not None:
                try:
                    sent_files = (
                        await attachment_ops.aresolve_attachment_sources(client, file_specs)
                        if file_specs is not None
                        else []
                    )
                except ValueError as e:
                    return str(e)
                try:
                    sent_images = (
                        await attachment_ops.aresolve_attachment_sources(client, image_specs)
                        if image_specs is not None
                        else []
                    )
                except ValueError as e:
                    # The resolver names the key it was given; this list is "images".
                    return re.sub(r"^attachments\b", "images", str(e))
                try:
                    await teams_ops.asend_message_with_files(
                        client,
                        content=message,
                        content_type=content_type,
                        mentions=mentions_payload,
                        files=sent_files,
                        images=sent_images,
                        chat_id=chat_id,
                        team_id="" if chat_id else team_id,
                        channel_id="" if chat_id else channel_id,
                        exclude_user_id=teams_ops.decode_token_claims(token).get("oid", ""),
                    )
                except ValueError as e:
                    return str(e)
                summary = _teams_send_summary(bool(chat_id), sent_files, sent_images)
                return f"{summary}{everyone_note}"
            if chat_id:
                await teams_ops.asend_chat_message(
                    client,
                    chat_id,
                    message,
                    content_type=content_type,
                    mentions=mentions_payload,
                )
                return f"Message sent to Teams chat.{everyone_note}"
            else:
                await teams_ops.asend_channel_message(
                    client,
                    team_id,
                    channel_id,
                    message,
                    content_type=content_type,
                    mentions=mentions_payload,
                )
                return "Message sent to Teams channel."
    except TeamsNotAvailableError:
        return "Microsoft Teams is not available for this account."
    except FilesScopeMissingError:
        return (
            "**Files permission missing:** sending files into Teams uploads them to "
            "OneDrive first, which needs the Files.ReadWrite permission. This connection "
            "can only read files (org tenants: ask the admin to consent to "
            "Files.ReadWrite). Plain messages still work."
        )


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
        writer.writerow(
            [
                row["source"],
                row["source_name"],
                row["sender"],
                row["timestamp"],
                row["preview"],
            ]
        )
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
async def list_files(
    folder_path: str = "", site_id: str = "", query: str = "", url: str = "", top: int = 20
) -> str:
    """
    List or search files in OneDrive or SharePoint.

    Four modes depending on the parameters provided:
    - url set: lists children of a shared folder link (other params ignored)
    - query set: searches across all drives using Microsoft Search (folder_path ignored)
    - site_id set (no query): lists files in a SharePoint site's document library
    - neither set: lists files in the user's OneDrive

    Args:
        folder_path: Folder path to browse (e.g., "Documents/Reports"). Empty for root.
                     Ignored when query is provided.
        site_id: SharePoint site ID (from list_sharepoint_sites). Empty for OneDrive.
                 Ignored when query is provided.
        query: Search query (e.g., "Q4 budget"). When set, searches across all drives.
        url: A SharePoint/OneDrive sharing URL pointing to a folder. Lists its children.
        top: Maximum number of items to return (default: 20).
    """
    sharing_url = url.strip() if url else ""

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if sharing_url:
            try:
                items = await files_ops.alist_sharing_link_children(client, sharing_url, top=top)
            except GraphError as e:
                if e.status_code == 403:
                    return (
                        "**Access denied** to this sharing link.\n"
                        "You don't have permission to access this item. "
                        "The owner may need to re-share it with you."
                    )
                elif e.status_code == 404:
                    return (
                        "**Item not found** for this sharing link.\n"
                        "The link may have expired, been revoked, or the item was deleted."
                    )
                elif e.status_code == 400:
                    return (
                        "**Invalid sharing link.**\n"
                        "Could not resolve this URL. Make sure it's a valid "
                        "SharePoint or OneDrive sharing link."
                    )
                raise
            if not items:
                return "No files found in the shared folder."
            lines = [f"Found {len(items)} item(s) in shared folder:\n"]
            for item in items:
                lines.append(_format_drive_item(item))
            return "\n".join(lines)
        elif query:
            results = await files_ops.asearch_files_unified(client, query=query, top=top)
            if not results:
                return f'No files found matching "{query}".'
            lines = [f'Found {len(results)} result(s) for "{query}":\n']
            for i, item in enumerate(results, 1):
                name = item.get("name", "?")
                web_url = item.get("webUrl", "")
                summary = item.get("_searchSummary", "")
                size = _format_size(item.get("size", 0))
                lines.append(f"{i}. **{name}** ({size})\n   ID: `{item.get('id', '?')}`")
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
async def inspect_file(
    item_id: str = "", site_id: str = "", url: str = "", read_content: bool = False
) -> str:
    """
    Get metadata and optionally the content of a file from OneDrive or SharePoint.

    Accepts either:
    - item_id: A drive item ID (from list_files output)
    - url: A SharePoint/OneDrive sharing URL (paste directly from browser or Teams)

    By default returns metadata only (name, type, size, modified date, ID, URL).
    Pass read_content=True to also download and return the file's content.
    Supports text files (up to 2 MB) and Office documents (docx, pptx, xlsx, pdf
    up to 50 MB — extracts text, tables, and notes; images are noted but not shown).

    Args:
        item_id: The drive item ID (from list_files output). Can also accept a sharing URL.
        site_id: SharePoint site ID. Leave empty for OneDrive. Ignored when url is provided.
        url: A SharePoint/OneDrive sharing URL. When provided, resolves the link first.
        read_content: False (default) to return metadata only.
                      True to download and return text content.
    """
    sharing_url = url.strip() if url else ""
    if not sharing_url and item_id and files_ops.is_sharing_url(item_id):
        sharing_url = item_id.strip()

    if not sharing_url and not item_id:
        return "Please provide either an item_id or a sharing url."

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if sharing_url:
            try:
                if read_content:
                    item, content = await files_ops.aresolve_sharing_link_content(
                        client, sharing_url
                    )
                    if content is None:
                        item, content = await files_ops.aresolve_sharing_link_extracted_content(
                            client, sharing_url, item=item
                        )
                else:
                    item = await files_ops.aresolve_sharing_link(client, sharing_url)
                    content = None
            except GraphError as e:
                if e.status_code == 403:
                    return (
                        "**Access denied** to this sharing link.\n"
                        "You don't have permission to access this item. "
                        "The owner may need to re-share it with you."
                    )
                elif e.status_code == 404:
                    return (
                        "**Item not found** for this sharing link.\n"
                        "The link may have expired, been revoked, or the item was deleted."
                    )
                elif e.status_code == 400:
                    return (
                        "**Invalid sharing link.**\n"
                        "Could not resolve this URL. Make sure it's a valid "
                        "SharePoint or OneDrive sharing link."
                    )
                raise
        else:
            if read_content:
                item, content = await files_ops.aget_drive_item_content(
                    client, item_id, site_id=site_id
                )
                if content is None:
                    item, content = await files_ops.aget_drive_item_extracted_content(
                        client, item_id, site_id=site_id, item=item
                    )
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

    parent_ref = item.get("parentReference", {})
    header = (
        f"**Name:** {name}\n"
        f"{type_line}\n"
        f"**Modified:** {modified} by {modified_by}\n"
        f"**ID:** `{item.get('id', '?')}`"
    )
    if parent_ref.get("driveId"):
        header += f"\n**Drive ID:** `{parent_ref['driveId']}`"
    if web_url:
        header += f"\n**URL:** {web_url}"

    if not read_content:
        return header

    if content is not None:
        return f"{header}\n\n---\n{content}"

    if "folder" in item:
        msg = "This is a folder, not a file. Use list_files to browse its contents."
    elif item.get("size", 0) > files_ops.MAX_DOCUMENT_DOWNLOAD_BYTES:
        msg = "This file is too large for content extraction (limit: 50 MB)."
    elif item.get("size", 0) > files_ops.MAX_TEXT_DOWNLOAD_BYTES:
        msg = "This file is too large to display as text (limit: 2 MB)."
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
    content_encoding: str = "",
) -> str:
    """
    Create or overwrite a file in OneDrive or SharePoint.

    Uses the simple upload endpoint (max 4 MB). The file is created if it does
    not exist, or overwritten if it does.

    Supported modes:
      - Text files (.txt, .md, .html, .csv, .json, .xml, .yaml): provide plain
        text content directly.
      - Word documents (.docx): provide content as markdown text. The server
        automatically converts markdown (headings, bold, italic, lists, tables)
        into a formatted .docx file. Write the document content using normal
        markdown syntax: # Heading, **bold**, *italic*, - bullets, 1. numbered,
        and pipe tables.
      - Excel workbooks (.xlsx): provide content as CSV text (comma-separated
        rows) to seed the first sheet, or an empty string for a blank workbook.
        Numeric-looking cells become numbers. Use edit_document afterwards for
        richer, in-place edits.
      - Binary files (any extension): set content_encoding="base64" and provide
        the file content as a base64-encoded string. Use this for images, PDFs,
        or other binary formats that originate from another source.

    Args:
        filename: File name including extension (e.g. "report.md", "Review.docx").
        content: File content — plain text, markdown (.docx), CSV (.xlsx), or base64 string.
        folder_path: Destination folder path (e.g. "Documents" or
            "Shared Documents/Templates"). Empty string uploads to the drive root.
        site_id: SharePoint site ID (from list_sharepoint_sites). Empty for OneDrive.
        content_encoding: Set to "base64" when content is base64-encoded binary data.
            Leave empty for text, markdown, or CSV content.
    """
    is_base64 = content_encoding.lower() == "base64" if content_encoding else False
    lower_name = filename.lower()
    is_docx = lower_name.endswith(".docx") and not is_base64
    is_xlsx = lower_name.endswith(".xlsx") and not is_base64

    if is_base64:
        try:
            data = base64.b64decode(content)
        except Exception as e:
            return f"Failed to decode base64 content: {e}"
    elif is_docx:
        try:
            data = document_create.markdown_to_docx(content)
        except ValueError as e:
            return f"Failed to generate Word document: {e}"
    elif is_xlsx:
        try:
            data = document_create.csv_to_xlsx(content)
        except ValueError as e:
            return f"Failed to generate Excel workbook: {e}"

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        if is_base64:
            item = await files_ops.aupload_bytes(
                client,
                folder_path=folder_path,
                filename=filename,
                data=data,
                content_type="application/octet-stream",
                site_id=site_id,
            )
        elif is_docx:
            item = await files_ops.aupload_bytes(
                client,
                folder_path=folder_path,
                filename=filename,
                data=data,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                site_id=site_id,
            )
        elif is_xlsx:
            item = await files_ops.aupload_bytes(
                client,
                folder_path=folder_path,
                filename=filename,
                data=data,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                site_id=site_id,
            )
        else:
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


def _op_summary(op_types: list[str]) -> str:
    """Human-readable summary of applied operations, capped at 5 names."""
    summary = ", ".join(op_types[:5])
    if len(op_types) > 5:
        summary += f" (+{len(op_types) - 5} more)"
    return summary


@mcp.tool()
async def edit_document(
    item_id: str,
    edits: str,
    site_id: str = "",
    options: str = "",
) -> str:
    """
    Edit an existing Word document (.docx) or Excel workbook (.xlsx) in
    OneDrive or SharePoint. The file type is detected from its extension.

    WORD (.docx): downloads, applies edits, and re-uploads. By default edits
    appear as Track Changes (revisions visible to reviewers); set track_changes
    to false in options to overwrite directly. Edits is a JSON array of ops:
      [
        {"op": "replace", "find": "old text", "replace": "new text"},
        {"op": "append", "content": "new paragraph text"},
        {"op": "insert_after", "after": "paragraph to find", "content": "new paragraph"},
        {"op": "delete", "find": "paragraph text to remove"},
        {"op": "comment", "find": "text to annotate", "comment": "reviewer note"}
      ]

    EXCEL (.xlsx): edits are applied in place via the Microsoft Graph Workbook
    API — the file is never downloaded or overwritten, so formulas recalculate
    and existing charts, pivot tables, formatting, and other sheets are
    preserved. Each op may include an optional "sheet" (defaults to the first
    worksheet). Edits is a JSON array of ops:
      [
        {"op": "set_cell", "cell": "B2", "value": "42"},
        {"op": "set_range", "range": "A1:B2", "values": [["Name","Qty"],["Widget","10"]]},
        {"op": "add_column", "header": "Total", "values": ["=A2*B2","=A3*B3"]},
        {"op": "insert_rows", "at": 5, "count": 2},
        {"op": "delete_rows", "at": 10},
        {"op": "insert_columns", "at": 3},
        {"op": "delete_columns", "at": 3, "count": 2}
      ]
    For Excel: "values" (set_range) is a 2-D array; a cell whose string starts
    with "=" is a formula. add_column appends after the used range. "at" is
    1-based (row 1 = first row, column 1 = A); "count" defaults to 1.

    Excel edits are NOT atomic across a batch: each op commits as it is applied,
    so if the call fails partway (e.g. a network drop), earlier ops persist and
    later ones do not. Re-running the remaining ops is safe. The workbook also
    holds a short lock (~1-2 min) after editing, so an immediate rename/copy/
    delete via manage_file may return "locked" — retry after a moment.

    Args:
        item_id: Drive item ID of the .docx or .xlsx file (from list_files or inspect_file).
        edits: JSON array of edit operations (see per-type shapes above).
        site_id: SharePoint site ID. Leave empty for OneDrive.
        options: JSON string with optional settings. Word only:
            {"track_changes": false} — apply edits directly without revision markup.
            {"author": "Name"} — override the revision/comment author (default: "Bond AI").
    """
    opts, err = parse_options(options)
    if err:
        return err

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        try:
            item = await files_ops.aget_drive_item(client, item_id, site_id=site_id)
        except GraphError as e:
            if e.status_code == 404:
                return f"File not found: {item_id}"
            return f"Error fetching file: {e}"

        name = item.get("name", "")
        lower = name.lower()
        if lower.endswith(".docx"):
            return await _edit_word(client, item, edits, site_id, opts)
        if lower.endswith(".xlsx"):
            return await _edit_excel(client, item, edits, site_id)
        return (
            f"File '{name}' is not an editable document. Supported types: "
            ".docx (Word) and .xlsx (Excel). Legacy .xls/.xlsb and macro-enabled "
            ".xlsm are not supported."
        )


async def _edit_word(
    client: AsyncGraphClient,
    item: dict,
    edits: str,
    site_id: str,
    opts: dict,
) -> str:
    """Edit a .docx via download / apply / re-upload (Track Changes by default)."""
    track_changes = opt_bool(opts.get("track_changes", True), True)
    author = opts.get("author", "Bond AI")

    try:
        operations = document_edit.parse_edits(edits)
    except ValueError as e:
        return f"Invalid edits: {e}"
    if not operations:
        return "No edit operations provided."

    name = item.get("name", "")
    base = files_ops._drive_base(site_id or None)
    doc_bytes = await client.get_bytes(f"{base}/items/{item['id']}/content")

    try:
        modified_bytes = document_edit.apply_edits(
            doc_bytes, operations, track_changes=track_changes, author=author
        )
    except document_edit.EditError as e:
        return f"Edit failed: {e}"

    result_item = await files_ops.aupload_bytes_by_id(
        client,
        item_id=item["id"],
        data=modified_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        site_id=site_id,
    )

    tc_note = " with Track Changes" if track_changes else ""
    result = f"Document '{name}' edited successfully{tc_note}."
    result += f"\n**Operations applied:** {len(operations)} ({_op_summary([op['op'] for op in operations])})"
    result += f"\n**Author:** {author}"
    result += f"\n**ID:** `{result_item.get('id', item['id'])}`"
    web_url = result_item.get("webUrl", "")
    if web_url:
        result += f"\n**URL:** {web_url}"
    return result


async def _edit_excel(
    client: AsyncGraphClient,
    item: dict,
    edits: str,
    site_id: str,
) -> str:
    """Edit an .xlsx in place via the Graph Workbook API (no download/re-upload)."""
    try:
        operations = workbook_edit.parse_workbook_edits(edits)
    except ValueError as e:
        return f"Invalid edits: {e}"
    if not operations:
        return "No edit operations provided."

    name = item.get("name", "")
    base = files_ops._drive_base(site_id or None)

    try:
        result = await workbook_edit.apply_workbook_edits(client, base, item["id"], operations)
    except workbook_edit.EditError as e:
        return f"Edit failed: {e}"
    except GraphError as e:
        return f"Edit failed: {e}"

    applied = result["operations"]
    out = f"Workbook '{name}' edited successfully in place."
    out += f"\n**Operations applied:** {len(applied)} ({_op_summary(applied)})"
    out += f"\n**Default sheet:** {result['default_sheet']}"
    out += f"\n**Worksheets:** {', '.join(result['worksheets'])}"
    web_url = item.get("webUrl", "")
    if web_url:
        out += f"\n**URL:** {web_url}"
    return out


@mcp.tool()
async def manage_file(
    item_id: str,
    action: str = "rename",
    new_name: str = "",
    options: str = "",
) -> str:
    """
    Copy, rename, or delete a file or folder.

    Actions:
      - "rename" (default): rename a file or folder in place. Requires new_name.
      - "copy": create a server-side copy with a new name — works for any file
        type including Word, Excel, and PDF. Useful for creating a new document
        from a template. Requires new_name.
      - "delete": move the file or folder to the recycle bin (recoverable from
        the SharePoint/OneDrive UI). new_name is ignored.

    Note: a workbook edited via edit_document holds a short lock (~1-2 min)
    afterward; rename/copy/delete on it may return "locked" until that clears —
    retry after a moment.

    Args:
        item_id: Drive item ID of the file or folder to act on (from list_files).
        action: "rename" (default), "copy", or "delete".
        new_name: New name including extension (e.g. "Final-Report.docx").
            Required for rename and copy; ignored for delete.
        options: JSON string with optional fields:
            {"destination_folder_id": "...", "site_id": "...", "destination_drive_id": "...", "source_drive_id": "..."}
    """
    opts, err = parse_options(options)
    if err:
        return err
    destination_folder_id = opts.get("destination_folder_id", "")
    site_id = opts.get("site_id", "")
    destination_drive_id = opts.get("destination_drive_id", "")
    source_drive_id = opts.get("source_drive_id", "")

    action = action.lower()
    if action not in ("rename", "copy", "delete"):
        return f"Invalid action '{action}'. Must be 'rename', 'copy', or 'delete'."
    if action in ("rename", "copy") and not new_name:
        return f"new_name is required for the '{action}' action."

    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        try:
            if action == "copy":
                status = await files_ops.acopy_drive_item(
                    client,
                    item_id=item_id,
                    new_name=new_name,
                    destination_folder_id=destination_folder_id,
                    site_id=site_id,
                    destination_drive_id=destination_drive_id,
                    source_drive_id=source_drive_id,
                )
                resource_id = status.get("resourceId", "?")
                return f"File copied successfully as '{new_name}'.\nNew item ID: `{resource_id}`"
            elif action == "delete":
                await files_ops.adelete_drive_item(client, item_id=item_id, site_id=site_id)
                return f"Deleted item `{item_id}` (moved to the recycle bin)."
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
        except GraphError as e:
            if e.status_code == 404:
                return f"File not found: {item_id}"
            return f"Error performing {action}: {e}"


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
        datasets = (
            await pbi_ops.alist_datasets(client, ws) if content_type in ("datasets", "all") else []
        )
        reports = (
            await pbi_ops.alist_reports(client, ws) if content_type in ("reports", "all") else []
        )
        dashboards = (
            await pbi_ops.alist_dashboards(client, ws)
            if content_type in ("dashboards", "all")
            else []
        )

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
            lines.append(
                f"  - **{r.get('name', '?')}** (ID: `{r.get('id', '?')}`, dataset: `{r.get('datasetId', '?')}`)"
            )
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
            f"because Microsoft auth is not active. "
            f"Run `make login-microsoft` (standalone) or connect your Microsoft account "
            f"in Bond AI Settings → Connections (backend mode) to enable OneDrive upload."
        )
    return result


# ---------------------------------------------------------------------------
# Desktop JSON tools
#
# A separate namespace for programmatic clients (the desktop mail app). Unlike
# the 28 markdown tools above, these return dicts — FastMCP renders them as
# structuredContent. Parameters remain str/int only.
#
# Error contract: a missing Microsoft connection returns the not_connected
# payload (with a connect URL when one exists). The Teams write tools return
# a structured "teams_unavailable" error for the no-license 403, which is
# permanent and must not be retried. The mail attachment tools likewise return
# structured permanent errors — invalid_mode, too_large, reference, empty_name,
# invalid_base64 — which must not be retried either. The Teams attachment
# reader returns not_found, access_denied, no_thumbnail, invalid_thumbnail,
# is_folder, and too_large; inspect_file_json returns missing_target,
# access_denied, not_found, and invalid_link — all permanent.
# send_chat_message_json returns invalid_attachments and files_scope_missing
# (the account's connection lacks Files.ReadWrite) — both permanent. The mail
# tools return external_sender when the mail sender policy hides a message —
# also permanent, and never accompanied by any detail of the hidden message;
# connection_status reports mail_policy.enabled so a client can explain the
# gap and resync when it flips. The three paging tools (list_mail_delta,
# list_chats_page, list_chat_messages_page) return invalid_cursor when the
# cursor is not a Graph URL — permanent; the Graph client refuses to send the
# bearer token anywhere else. send_draft reads the draft's ids before sending
# and returns them, so a client can store its own copy of the sent mail.
# search_people_json returns directory_scope_missing when the connection lacks
# User.ReadBasic.All; ensure_chat_json returns invalid_members (an id that is
# not a Graph user id or UPN), no_identity (the caller cannot be read off the
# token), and no_members (nobody left after dropping blanks and the caller),
# plus teams_unavailable — all permanent.
# Everything else — Graph 5xx, throttling, unexpected shapes, a malformed
# policy allowlist — propagates so FastMCP raises a tool error, which is the
# client's "transient, retry later" signal.
# ---------------------------------------------------------------------------


def _profile_json(profile: dict) -> dict:
    """Map a Graph /me payload to the desktop profile shape."""
    return {
        "id": profile.get("id"),
        "display_name": profile.get("displayName"),
        "mail": profile.get("mail"),
        "user_principal_name": profile.get("userPrincipalName"),
    }


def _person_json(user: dict) -> dict:
    """Map a Graph /users row to the desktop directory shape."""
    return {
        "id": user.get("id"),
        "display_name": user.get("displayName"),
        "mail": user.get("mail"),
        "user_principal_name": user.get("userPrincipalName"),
        "job_title": user.get("jobTitle"),
    }


def _chat_message_json(msg: dict) -> dict:
    """Flatten a Graph chatMessage. Every nested object can be null on system
    events, so each level is read defensively."""
    sender = msg.get("from") or {}
    user = sender.get("user") or {}
    application = sender.get("application") or {}
    body = msg.get("body") or {}
    # Only "who was named" travels: the desktop re-nests it. Channel/tag
    # mentions have no user under mentioned, so they drop out here. Each level
    # is type-checked rather than `or {}`-chained, because unlike the sender
    # above, a mention is a collection — one bad entry must not sink the page.
    mentioned_user_ids = []
    for mention in msg.get("mentions") or []:
        mentioned = mention.get("mentioned") if isinstance(mention, dict) else None
        mentioned_user = mentioned.get("user") if isinstance(mentioned, dict) else None
        mentioned_id = mentioned_user.get("id") if isinstance(mentioned_user, dict) else None
        if mentioned_id:
            mentioned_user_ids.append(mentioned_id)
    return {
        "id": msg.get("id"),
        "message_type": msg.get("messageType"),
        "from_user_id": user.get("id"),
        "from_user_display": user.get("displayName"),
        "from_application_id": application.get("id"),
        "body_content": body.get("content"),
        "body_content_type": body.get("contentType"),
        "mentioned_user_ids": mentioned_user_ids,
        "created": msg.get("createdDateTime"),
        "last_modified": msg.get("lastModifiedDateTime"),
        "attachments": teams_ops.parse_message_attachments(msg),
    }


def _recipients_json(recipients: Any) -> list[dict]:
    """Flatten Graph recipient objects to {name, address}. A malformed entry is
    skipped rather than sinking the whole draft."""
    out = []
    for entry in recipients or []:
        address = entry.get("emailAddress") if isinstance(entry, dict) else None
        if isinstance(address, dict) and address.get("address"):
            out.append({"name": address.get("name"), "address": address["address"]})
    return out


def _draft_json(draft: dict) -> dict:
    """The one shape a draft has, whether it answers a message or starts a thread."""
    return {
        "id": draft.get("id", ""),
        "web_link": draft.get("webLink", ""),
        "conversation_id": draft.get("conversationId"),
        "internet_message_id": draft.get("internetMessageId"),
        "subject": draft.get("subject"),
        "to": _recipients_json(draft.get("toRecipients")),
        "cc": _recipients_json(draft.get("ccRecipients")),
    }


def _utcnow_iso() -> str:
    """Server time as ISO-8601 UTC with a Z suffix; a function so tests can pin it."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stored_graph_scopes() -> list[str]:
    """Read the granted scopes off the stored Microsoft token row.

    Best-effort by design: laptop (MSAL) mode has no DB row at all, and a
    status probe must never be the thing that crashes. Any failure — no row,
    no DB, unexpected value type — reports as "unknown" (empty list).
    """
    try:
        from auth import resolve_user_key_for_request
        from auth.db.repository import TokenRepository

        data = TokenRepository().get_token(resolve_user_key_for_request(), "microsoft")
        raw = data.get("scopes") if data else None
        if not raw:
            return []
        # Graph echoes scopes fully qualified (https://graph.microsoft.com/Mail.Read);
        # the desktop client matches on the bare name.
        return [scope.rsplit("/", 1)[-1].lower() for scope in raw.split()]
    except Exception:
        logger.debug("Could not read stored Microsoft scopes", exc_info=True)
        return []


@mcp.tool()
async def get_profile_json() -> dict:
    """
    Get the signed-in user's identity as structured JSON.

    For programmatic clients. Returns id, display_name, mail, and
    user_principal_name. The id is the Graph user object ID — Teams messages
    carry the same ID, so clients use it to tell their own messages apart from
    everyone else's.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            profile = await mail_ops.aget_profile(client)
    except PermissionError as e:
        return _not_connected(e)
    return _profile_json(profile)


@mcp.tool()
async def search_people_json(query: str, top: int = 10) -> dict:
    """
    Search the organisation directory as structured JSON.

    For programmatic clients building a recipient typeahead. Matches people
    whose display name has a word starting with the query or whose mail
    starts with it, ordered by display name. Returns people: a list of
    {id, display_name, mail, user_principal_name, job_title}; mail and
    job_title may be null. The signed-in user can appear in the results —
    the client filters them out if it wants to. A blank query, or one with
    nothing searchable left once "&" is dropped, returns an empty list
    without calling Graph.

    Requires User.ReadBasic.All. Without it the tool returns
    {"error": "directory_scope_missing"}, which is permanent until the
    connection is widened (see connection_status.scopes). Throttling (429)
    propagates as a tool error; a typeahead should drop that request and keep
    its last results rather than retry.

    Args:
        query: Name or mail prefix to search for.
        top: Maximum results, 1-50 (default 10).
    """
    query = query.strip()
    if not query:
        return {"people": []}
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            users = await people_ops.asearch_users(client, query, top=top)
    except PermissionError as e:
        return _not_connected(e)
    except DirectoryScopeMissingError:
        return {"error": "directory_scope_missing"}
    return {"people": [_person_json(u) for u in users]}


@mcp.tool()
async def list_mail_delta(folder: str = "inbox", cursor: str = "", min_received: str = "") -> dict:
    """
    Fetch ONE page of a mail folder's delta feed as structured JSON.

    For programmatic clients doing incremental sync. Returns messages (raw
    Graph message objects, including `@removed` tombstones for deletions),
    next_cursor (more pages in this run), delta_cursor (this run is done —
    save it and pass it back next time), and resync.

    Args:
        folder: Mail folder to sync (default: inbox).
        cursor: A next_cursor or delta_cursor from a previous call. Empty
            starts a fresh enumeration.
        min_received: ISO 8601 timestamp bounding a fresh enumeration
            (e.g. "2026-01-01T00:00:00Z"). Ignored when cursor is set.

    A resync of true means the saved cursor has expired: discard local state
    for the folder and call again with an empty cursor. A cursor that is not a
    Graph URL returns {"error": "invalid_cursor"} without any request: cursors
    only ever come from this tool, and the token must not follow one elsewhere.

    While the mail sender policy is on, messages from senders outside the
    allowed domains are omitted from messages; `@removed` tombstones always
    pass through. A policy change needs a full resync to take effect on
    already-synced rows.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            data = await mail_ops.adelta_page(
                client, folder=folder, cursor=cursor, min_received=min_received
            )
    except PermissionError as e:
        return _not_connected(e)
    except NonGraphUrlError:
        logger.warning("list_mail_delta: refused a non-Graph cursor")
        return {"error": "invalid_cursor"}
    except GraphError as e:
        if e.status_code == 410:
            return {"messages": [], "next_cursor": "", "delta_cursor": "", "resync": True}
        raise

    return {
        "messages": mail_policy.filter_messages(data.get("value", [])),
        "next_cursor": data.get("@odata.nextLink", ""),
        "delta_cursor": data.get("@odata.deltaLink", ""),
        "resync": False,
    }


@mcp.tool()
async def get_mail_detail(message_id: str) -> dict:
    """
    Get a message's plain-text body, internet headers, and attachment list.

    For programmatic clients. Exchange converts the body to text server-side,
    so the client never parses HTML. Returns body_text (the reply-relevant
    part only, without the quoted thread), headers (lowercased name → value;
    where a header repeats, the first occurrence wins), and has_attachments.

    Also returns attachments: metadata only — id, name, content_type, size (in
    bytes), is_inline, content_id, kind (file | item | reference | unknown), and
    source_url for link attachments. No bytes are fetched here; pass an id to
    get_mail_attachment_json for content. At most 50 attachments are listed,
    while attachment_count reports the true number.

    While the mail sender policy is on, a message from a sender outside the
    allowed domains returns {"error": "external_sender"} and nothing else.

    Args:
        message_id: The Graph message ID.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            msg = await mail_ops.aget_message_detail(client, message_id)
            if not mail_policy.message_allowed(msg):
                return {"error": mail_policy.EXTERNAL_SENDER_ERROR}
    except PermissionError as e:
        return _not_connected(e)

    headers: dict[str, str] = {}
    for header in msg.get("internetMessageHeaders") or []:
        name = (header.get("name") or "").lower()
        if name and name not in headers:
            headers[name] = header.get("value")

    # aget_message_detail already $expands the attachments, so the list costs
    # no extra round trip.
    raw = msg.get("attachments") or []
    return {
        "body_text": (msg.get("uniqueBody") or {}).get("content") or "",
        "headers": headers,
        "has_attachments": bool(msg.get("hasAttachments")),
        "attachments": [
            attachment_ops.attachment_summary(a)
            for a in raw[: attachment_ops.MAX_LISTED_ATTACHMENTS]
        ],
        "attachment_count": len(raw),
    }


async def _attachment_json_text(
    client: AsyncGraphClient,
    message_id: str,
    attachment_id: str,
    summary: dict,
    expanded: dict | None = None,
) -> dict:
    """text mode for get_mail_attachment_json: extract, decode, or explain.

    ``expanded`` is the already-fetched item attachment when the mail policy
    had to fetch it to judge the attached message, so it is never fetched twice.
    """
    if summary["kind"] == "reference":
        return {**summary, "text": None, "truncated": False, "reason": "reference"}
    if summary["kind"] == "item":
        if expanded is None:
            expanded = await attachment_ops.aget_item_attachment(client, message_id, attachment_id)
        item = expanded.get("item") or {}
        body = item.get("body") or {}
        is_text = body.get("contentType") == "text"
        return {
            **summary,
            **_attachment_item_fields(item),
            "text": body.get("content", "") if is_text else item.get("bodyPreview", ""),
            "truncated": not is_text,
        }
    if summary["size"] > files_ops.MAX_DOCUMENT_DOWNLOAD_BYTES:
        return {**summary, "text": None, "truncated": False, "reason": "too_large"}

    data, header_type = await attachment_ops.aget_attachment_bytes(
        client, message_id, attachment_id
    )
    name = summary["name"] or attachment_id
    content_type = summary["content_type"] or header_type or attachment_ops.guess_content_type(name)
    summary["content_type"] = summary["content_type"] or content_type
    att = attachment_ops.ResolvedAttachment(name=name, data=data, content_type=content_type)
    result = await attachment_ops.adeliver_attachment(client, att, "text")
    out = {**summary, "text": result["text"], "truncated": result["truncated"]}
    if "reason" in result:
        out["reason"] = result["reason"]
    return out


def _attachment_item_fields(item: dict) -> dict:
    """The inner-message fields an item attachment contributes to a JSON result."""
    sender = (item.get("from") or {}).get("emailAddress") or {}
    return {
        "item_subject": item.get("subject"),
        "item_from": sender.get("address"),
        "item_received": item.get("receivedDateTime"),
    }


@mcp.tool()
async def get_mail_attachment_json(
    message_id: str, attachment_id: str, mode: str = "bytes"
) -> dict:
    """
    Get one email attachment's metadata, extracted text, or raw bytes.

    For programmatic clients. Get the IDs from get_mail_detail, which lists a
    message's attachments.

    Args:
        message_id: The Graph message ID.
        attachment_id: The attachment ID (from get_mail_detail).
        mode: "bytes" (default) returns content_base64 alongside the metadata,
            for attachments up to 10 MB. "metadata" returns the summary only and
            fetches no content. "text" returns extracted text (Word, PowerPoint,
            Excel, PDF) or decoded text, with truncated and, when there is no
            text, reason ("binary" | "unsupported" | "too_large" | "reference").

    Returns:
        The attachment summary (id, name, content_type, size, is_inline,
        content_id, kind, source_url) plus whatever the mode adds. An attached
        message (kind "item") also carries item_subject, item_from, and
        item_received in metadata and text modes, and downloads as .eml bytes.

        Permanent errors, which must not be retried: external_sender (the mail
        sender policy hides this message or the message attached to it),
        invalid_mode (mode was not
        one of the three), too_large (with size and limit; bytes mode only,
        decided from the metadata so nothing is downloaded), reference (with
        source_url; a link attachment has no bytes), not_connected (with
        connect_url when one exists). Everything else — a Graph 404 for an
        unknown message or attachment, throttling, 5xx — propagates as a tool
        error, which is the client's "transient, retry later" signal.
    """
    mode = mode.strip().lower() or "bytes"
    if mode not in ("metadata", "text", "bytes"):
        return {"error": "invalid_mode"}

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            # Judge the parent message before any attachment metadata or bytes
            # are read. Inside the try so a malformed policy allowlist
            # propagates as a tool error rather than being mistaken for a
            # missing connection.
            if not await mail_policy.acheck_message(client, message_id, None):
                return {"error": mail_policy.EXTERNAL_SENDER_ERROR}

            meta = await attachment_ops.aget_attachment_metadata(client, message_id, attachment_id)
            summary = attachment_ops.attachment_summary(meta)

            # An attached message is judged by the same rule; fetched once here
            # and handed to whichever mode needs it.
            expanded: dict | None = None
            if summary["kind"] == "item" and mail_policy.enabled():
                expanded = await attachment_ops.aget_item_attachment(
                    client, message_id, attachment_id
                )
                if not mail_policy.message_allowed(expanded.get("item") or {}):
                    return {"error": mail_policy.EXTERNAL_SENDER_ERROR}

            if mode == "metadata":
                if summary["kind"] != "item":
                    return summary
                if expanded is None:
                    expanded = await attachment_ops.aget_item_attachment(
                        client, message_id, attachment_id
                    )
                return {**summary, **_attachment_item_fields(expanded.get("item") or {})}

            if mode == "text":
                return await _attachment_json_text(
                    client, message_id, attachment_id, summary, expanded
                )

            if summary["kind"] == "reference":
                return {"error": "reference", "source_url": summary["source_url"]}
            if summary["size"] > attachment_ops.MAX_JSON_ATTACHMENT_BYTES:
                return {
                    "error": "too_large",
                    "size": summary["size"],
                    "limit": attachment_ops.MAX_JSON_ATTACHMENT_BYTES,
                }
            data, header_type = await attachment_ops.aget_attachment_bytes(
                client, message_id, attachment_id
            )
    except PermissionError as e:
        return _not_connected(e)

    if not summary["content_type"]:
        fallback = "message/rfc822" if summary["kind"] == "item" else ""
        summary["content_type"] = header_type or fallback
    return {**summary, "content_base64": base64.b64encode(data).decode("ascii")}


@mcp.tool()
async def create_reply_draft_json(message_id: str, timezone: str = "") -> dict:
    """
    Create a reply draft for a message and return its ID as structured JSON.

    For programmatic clients. Graph builds the draft with recipients and the
    quoted original already filled in; use update_draft_body to set the reply
    text, then send_draft. Returns the draft's id, web_link, conversation_id,
    internet_message_id, subject, to, and cc — the same shape create_draft_json
    returns.

    Args:
        message_id: The Graph message ID to reply to.
        timezone: IANA or Windows timezone for the quoted original's
            timestamps (e.g. "America/New_York"). Empty leaves them in UTC.

    While the mail sender policy is on, replying to a message from a sender
    outside the allowed domains returns {"error": "external_sender"} — Graph
    would otherwise quote the original into a draft whose from is the user.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            if not await mail_policy.acheck_message(client, message_id, None):
                return {"error": mail_policy.EXTERNAL_SENDER_ERROR}
            draft = await mail_ops.acreate_reply_draft(client, message_id, timezone=timezone)
    except PermissionError as e:
        return _not_connected(e)
    return _draft_json(draft)


@mcp.tool()
async def create_draft_json(
    to: str, subject: str, body: str = "", cc: str = "", bcc: str = ""
) -> dict:
    """
    Create a new mail draft with recipients and a body. Returns structured JSON.

    For programmatic clients composing a fresh message rather than a reply. The
    body is written as plain text (contentType "text") — the same rule
    update_draft_body holds, so a typed "<" stays a "<" instead of becoming
    markup. An empty ``to`` is accepted, which lets a client save a skeleton
    draft and let the user finish it in Outlook through web_link. The draft's id
    then works unchanged with add_draft_attachment_json, update_draft_body, and
    send_draft.

    Args:
        to: Recipient addresses, comma-separated. Empty is allowed.
        subject: The subject line.
        body: Plain-text body. Empty leaves the draft body blank.
        cc: Cc addresses, comma-separated.
        bcc: Bcc addresses, comma-separated.

    Returns:
        The same shape create_reply_draft_json returns — id, web_link,
        conversation_id, internet_message_id, subject, to, and cc.

        The mail sender policy does not gate this tool: it composes outbound
        mail of the user's own, and reads no message that arrived from anyone.
        not_connected (with connect_url when one exists) is returned when there
        is no Microsoft connection; everything else propagates as a tool error.
    """
    to_list = [addr.strip() for addr in to.split(",") if addr.strip()]
    cc_list = [addr.strip() for addr in cc.split(",") if addr.strip()]
    bcc_list = [addr.strip() for addr in bcc.split(",") if addr.strip()]
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            # None, not [] — the payload builder omits an absent cc/bcc and Graph
            # treats an explicitly empty one differently.
            draft = await mail_ops.acreate_draft(
                client,
                to=to_list,
                subject=subject,
                body=body,
                cc=cc_list or None,
                bcc=bcc_list or None,
                body_type="Text",
            )
    except PermissionError as e:
        return _not_connected(e)
    return _draft_json(draft)


@mcp.tool()
async def update_draft_body(draft_id: str, text: str) -> dict:
    """
    Replace a draft's body with plain text. Returns structured JSON.

    For programmatic clients. Overwrites the whole body, including anything
    Graph pre-filled — quote the original yourself if you want it kept.

    Args:
        draft_id: The draft message ID (from create_reply_draft_json).
        text: The plain-text body to write.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            await mail_ops.aupdate_draft_body(client, draft_id, text)
    except PermissionError as e:
        return _not_connected(e)
    return {"ok": True}


@mcp.tool()
async def add_draft_attachment_json(
    draft_id: str, name: str, content_base64: str, content_type: str = ""
) -> dict:
    """
    Attach a file to an existing draft. Returns structured JSON.

    For programmatic clients. Call this on a draft from create_reply_draft_json,
    after update_draft_body and before send_draft; a sent message can no longer
    take attachments. The server has no file system, so the bytes arrive
    base64-encoded. Files up to 150 MB are accepted — anything at or above 3 MB
    goes through a chunked upload session automatically, which takes longer.

    Args:
        draft_id: The draft message ID (from create_reply_draft_json).
        name: The file name shown in the message (e.g. "notes.txt"). Required.
        content_base64: The file's bytes, base64-encoded.
        content_type: MIME type. Empty guesses it from the name's extension.

    Returns:
        attachment_id — the new attachment's Graph ID.

        Permanent errors, which must not be retried: empty_name, invalid_base64
        (content_base64 was not valid base64, or decoded to nothing), too_large
        (with size and limit), not_connected (with connect_url when one exists).
        Everything else propagates as a tool error, signalling a retry.
    """
    name = name.strip()
    if not name:
        return {"error": "empty_name"}
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        return {"error": "invalid_base64"}
    if not data:
        return {"error": "invalid_base64"}
    if len(data) > attachment_ops.MAX_ATTACHMENT_BYTES:
        return {
            "error": "too_large",
            "size": len(data),
            "limit": attachment_ops.MAX_ATTACHMENT_BYTES,
        }
    ctype = content_type.strip() or attachment_ops.guess_content_type(name)

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            attachment_id = await attachment_ops.aadd_file_attachment(
                client, draft_id, name, data, ctype
            )
    except PermissionError as e:
        return _not_connected(e)
    return {"attachment_id": attachment_id}


@mcp.tool()
async def send_draft(draft_id: str) -> dict:
    """
    Send an existing draft. Returns structured JSON.

    For programmatic clients. Graph accepts the send asynchronously, so a
    successful return means "queued", not "delivered".

    The draft's ids and recipients are read first, then the draft is sent: once
    Exchange moves the copy to Sent Items the draft id stops resolving, so this
    is the only moment they can be learned. A failed read sends nothing and
    propagates as a tool error, which is safe to retry.

    Args:
        draft_id: The draft message ID (from create_reply_draft_json or
            create_draft_json).

    Returns:
        ok, plus the draft's id, conversation_id, internet_message_id, subject,
        to, cc, and sent_at. The id is the draft's and stops resolving once the
        copy lands in Sent Items, so it serves only as a client-side key;
        internet_message_id and conversation_id carry over to the sent copy, so
        a client can store its own copy of the mail immediately and later match
        it to the real Sent Items copy by internet_message_id. sent_at is the
        server's UTC clock when Graph queued the send; Exchange's own
        sentDateTime may differ from it by seconds. There is no web_link — the
        draft's deep link dies with the draft.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            draft = await mail_ops.aget_draft_for_send(client, draft_id)
            await mail_ops.asend_draft(client, draft_id)
    except PermissionError as e:
        return _not_connected(e)
    sent = _draft_json(draft)
    return {
        "ok": True,
        "id": sent["id"],
        "conversation_id": sent["conversation_id"],
        "internet_message_id": sent["internet_message_id"],
        "subject": sent["subject"],
        "to": sent["to"],
        "cc": sent["cc"],
        "sent_at": _utcnow_iso(),
    }


@mcp.tool()
async def mark_mail_read_json(message_ids: str, is_read: str = "true") -> dict:
    """
    Mark messages read (or unread) in bulk. Returns structured JSON.

    For programmatic clients syncing read state. Best effort per message: a
    message that no longer exists is reported in failed rather than failing the
    whole call. Returns updated (how many were patched) and failed (one entry
    per message that was not, with id and error). At most 100 IDs are processed
    per call; anything beyond that is ignored.

    Args:
        message_ids: JSON array of Graph message IDs, as a string, e.g.
            '["AAMkAGI2...", "AAMkAGI3..."]'.
        is_read: "true" (default) marks read, "false" marks unread.
    """
    import json

    try:
        ids = json.loads(message_ids)
    except (json.JSONDecodeError, TypeError):
        ids = None
    if not isinstance(ids, list) or not all(isinstance(mid, str) for mid in ids):
        return {"updated": 0, "failed": [], "error": "message_ids must be a JSON array of strings"}

    flag = is_read.strip().lower()
    if flag not in ("true", "false"):
        return {"updated": 0, "failed": [], "error": 'is_read must be "true" or "false"'}

    updated = 0
    failed = []
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            for message_id in ids[:100]:
                try:
                    await mail_ops.amark_read(client, message_id, is_read=flag == "true")
                except GraphError as e:
                    failed.append({"id": message_id, "error": str(e)})
                else:
                    updated += 1
    except PermissionError as e:
        return _not_connected(e)

    return {"updated": updated, "failed": failed}


@mcp.tool()
async def list_chats_page(cursor: str = "", top: int = 50) -> dict:
    """
    Fetch ONE page of the user's Teams chats as structured JSON.

    For programmatic clients. Chats come back newest-activity-first. Each entry
    has id, topic (null for 1:1 chats — resolve a name via
    get_chat_members_json), last_preview_at, and last_read_at (how far the
    signed-in user has read the chat; null when Graph sends no viewpoint).
    Returns next_cursor for the next page, empty when the listing is complete.
    A cursor that is not a Graph URL returns {"error": "invalid_cursor"} without
    any request.

    Args:
        cursor: A next_cursor from a previous call. Empty starts at page one.
        top: Page size (default: 50). Ignored when cursor is set.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            data = await teams_ops.achats_page(client, cursor=cursor, top=top)
    except PermissionError as e:
        return _not_connected(e)
    except NonGraphUrlError:
        logger.warning("list_chats_page: refused a non-Graph cursor")
        return {"error": "invalid_cursor"}

    chats = []
    for chat in data.get("value", []):
        preview = chat.get("lastMessagePreview") or {}
        viewpoint = chat.get("viewpoint") or {}
        chats.append(
            {
                "id": chat.get("id"),
                "topic": chat.get("topic"),
                "last_preview_at": preview.get("createdDateTime"),
                "last_read_at": viewpoint.get("lastMessageReadDateTime"),
            }
        )
    return {"chats": chats, "next_cursor": data.get("@odata.nextLink", "")}


@mcp.tool()
async def get_chat_members_json(chat_id: str) -> dict:
    """
    List a chat's members as structured JSON.

    For programmatic clients. Returns the chat's full member list, each entry
    with user_id and display_name. Use it to label 1:1 chats, which have no
    topic.

    Args:
        chat_id: The chat ID (from list_chats_page).
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            data = await teams_ops.achat_members(client, chat_id)
    except PermissionError as e:
        return _not_connected(e)

    members = [
        {"user_id": m.get("userId"), "display_name": m.get("displayName")}
        for m in data.get("value", [])
    ]
    return {"members": members}


_CHAT_MEMBER_ID_RE = re.compile(r"[A-Za-z0-9._@+-]+")


@mcp.tool()
async def ensure_chat_json(user_ids: str, topic: str = "") -> dict:
    """
    Find or create a Teams chat with the given people. Returns structured JSON.

    For programmatic clients that want to message someone who has no chat
    yet. Pass one id and Graph returns the existing 1:1 chat with that person
    if there is one, otherwise creates it — calling this twice is safe. Pass
    two or more ids and Graph creates a NEW group chat every call (group
    chats are never de-duplicated); topic applies only to a group chat. The
    signed-in user is always a member and need not be listed. Requires
    Chat.ReadWrite.

    Returns chat_id and chat_type ("oneOnOne" or "group"). Permanent errors:
    invalid_members (an id is not a Graph user id or UPN), no_identity (the
    signed-in user cannot be read off the token), no_members (nobody left
    after dropping blanks and the caller), teams_unavailable (no Teams
    license). A Graph 400 on an unknown user propagates as a tool error.

    Args:
        user_ids: Comma-separated Graph user ids or user principal names.
            Prefer the id search_people_json returns: a UPN with a character
            outside letters, digits, and ._@+- (an apostrophe, say) is
            rejected as invalid_members.
        topic: Optional group-chat title; ignored for a 1:1 chat.
    """
    others: list[str] = []
    for raw in user_ids.split(","):
        member = raw.strip()
        if member and member not in others:
            others.append(member)
    if any(not _CHAT_MEMBER_ID_RE.fullmatch(member) for member in others):
        return {"error": "invalid_members"}

    try:
        token = get_graph_token()
        oid = teams_ops.decode_token_claims(token)["oid"]
        if not oid:
            return {"error": "no_identity"}
        others = [member for member in others if member != oid]
        if not others:
            return {"error": "no_members"}
        async with AsyncGraphClient(token) as client:
            chat = await teams_ops.acreate_chat(client, [oid, *others], topic=topic.strip())
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {"error": "teams_unavailable"}

    return {"chat_id": chat.get("id"), "chat_type": chat.get("chatType")}


@mcp.tool()
async def list_chat_messages_page(chat_id: str, since: str = "", cursor: str = "") -> dict:
    """
    Fetch ONE page of a chat's messages as structured JSON.

    For programmatic clients. Messages come back newest-first and flattened:
    id, message_type, from_user_id, from_user_display, from_application_id,
    body_content, body_content_type, mentioned_user_ids, created,
    last_modified, attachments. System events have no sender, so the from_*
    fields are null. mentioned_user_ids is the Graph user ids the message
    @mentions, in the order they appear and empty when none. Returns
    next_cursor for the next page, empty when there are no more. A cursor that
    is not a Graph URL returns {"error": "invalid_cursor"} without any request.

    attachments: every file, inline image, card, or quoted-message reference on
    the message as {id, kind, name, content_type, content_url, thumbnail_url,
    card_text}; kind is one of file, image, card, message_reference, other;
    empty when none. Fetch a file's or image's bytes with
    get_chat_attachment_json. body_content is still the raw Graph body (the
    client owns stripping), so a file-only message has an empty or tag-only
    body — render the attachments list.

    Args:
        chat_id: The chat ID (from list_chats_page).
        since: ISO 8601 timestamp; returns only messages modified after it.
            Compare against the last_modified you stored, not created — an
            edited message resurfaces. Ignored when cursor is set.
        cursor: A next_cursor from a previous call. Empty starts at page one.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            data = await teams_ops.achat_messages_page(client, chat_id, since=since, cursor=cursor)
    except PermissionError as e:
        return _not_connected(e)
    except NonGraphUrlError:
        logger.warning("list_chat_messages_page: refused a non-Graph cursor")
        return {"error": "invalid_cursor"}

    return {
        "messages": [_chat_message_json(m) for m in data.get("value", [])],
        "next_cursor": data.get("@odata.nextLink", ""),
    }


@mcp.tool()
async def get_chat_attachment_json(
    chat_id: str, message_id: str, attachment_id: str, thumbnail: str = ""
) -> dict:
    """
    Get one Teams chat attachment's bytes as base64. Returns structured JSON.

    For programmatic clients. The ids come from list_chat_messages_page's
    attachments list: pass the entry's id for a shared file or an inline image.

    thumbnail: empty (default) returns the full bytes. "small", "medium", or
    "large" returns the file's driveItem thumbnail instead — files only; inline
    images are already small and ignore it.

    Returns kind, name, content_type, size, and content_base64, where size is
    the byte count of what was returned.

    Permanent errors, which must not be retried: invalid_thumbnail (not one of
    the three size words; checked before any request), not_found (the id is not
    on the message, or it is a card, a quoted reference, an unknown kind, or a
    file entry with no URL), access_denied (Graph 403 resolving the file's
    sharing link), no_thumbnail (the file has no thumbnail at that size),
    is_folder (the shared item is a folder), too_large (with size and limit —
    decided from the driveItem size before downloading a file, or from the byte
    count after fetching an image or a thumbnail), teams_unavailable (403 on
    the message itself), and not_connected. A Graph 404 for an unknown chat or
    message, throttling, and 5xx propagate as tool errors instead.

    Args:
        chat_id: The chat ID (from list_chats_page).
        message_id: The message ID (from list_chat_messages_page).
        attachment_id: The attachment entry's id from that message.
        thumbnail: "", "small", "medium", or "large".
    """
    thumb = thumbnail.strip().lower()
    if thumb and thumb not in _THUMBNAIL_WORDS:
        return {"error": "invalid_thumbnail"}

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            entry, _ = await _find_teams_attachment(
                client, message_id, attachment_id, chat_id=chat_id
            )
            if entry is None or entry["kind"] not in ("file", "image"):
                return {"error": "not_found"}

            if entry["kind"] == "image":
                data, header_type = await teams_ops.aget_hosted_content(
                    client, message_id, entry["id"], chat_id=chat_id
                )
                content_type = _mime_from_header(header_type, "application/octet-stream")
                name = _hosted_image_name(entry["id"], content_type)
            else:
                url = entry["content_url"]
                if not url:
                    return {"error": "not_found"}
                try:
                    if thumb:
                        found = await files_ops.aget_sharing_link_thumbnail(client, url, size=thumb)
                        if found is None:
                            return {"error": "no_thumbnail"}
                        data, header_type = found
                        content_type = _mime_from_header(header_type, "image/jpeg")
                        name = entry["name"]
                    else:
                        item = await files_ops.aresolve_sharing_link(client, url)
                        if "folder" in item:
                            return {"error": "is_folder"}
                        size = item.get("size", 0)
                        if (
                            isinstance(size, int)
                            and size > attachment_ops.MAX_JSON_ATTACHMENT_BYTES
                        ):
                            return {
                                "error": "too_large",
                                "size": size,
                                "limit": attachment_ops.MAX_JSON_ATTACHMENT_BYTES,
                            }
                        _, data = await files_ops.aresolve_sharing_link_bytes(
                            client, url, item=item
                        )
                        name = entry["name"] or item.get("name")
                        content_type = (item.get("file") or {}).get(
                            "mimeType"
                        ) or attachment_ops.guess_content_type(name or "")
                except GraphError as e:
                    if e.status_code == 403:
                        return {"error": "access_denied"}
                    raise
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {"error": "teams_unavailable"}

    # Images and thumbnails announce no size up front, so the cap is enforced
    # again here on what actually arrived.
    if len(data) > attachment_ops.MAX_JSON_ATTACHMENT_BYTES:
        return {
            "error": "too_large",
            "size": len(data),
            "limit": attachment_ops.MAX_JSON_ATTACHMENT_BYTES,
        }

    return {
        "kind": entry["kind"],
        "name": name,
        "content_type": content_type,
        "size": len(data),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


@mcp.tool()
async def mark_chat_read_json(chat_id: str) -> dict:
    """
    Mark a Teams chat read for the signed-in user. Returns structured JSON.

    Read state in Teams is per CHAT, not per message: it is a viewpoint on the
    conversation, so this marks the chat read up to its newest message and
    there is no way to ack one message and leave a later one unread. For
    programmatic clients acking a read the client already recorded locally.
    Requires Chat.ReadWrite.

    Returns ok: true on success. On failure ok is false and error says why —
    "no_identity" when the signed-in user cannot be read off the token,
    "teams_unavailable" when the account has no Teams license.

    Args:
        chat_id: The chat ID (from list_chats_page).
    """
    if not chat_id.strip():
        return {"ok": False, "error": "chat_id must not be empty"}

    try:
        token = get_graph_token()
        # Graph wants the user explicitly; a blank oid/tid would be a call that
        # marks nothing, so it is an error here rather than a request.
        claims = teams_ops.decode_token_claims(token)
        if not claims["oid"] or not claims["tid"]:
            return {"ok": False, "error": "no_identity"}
        async with AsyncGraphClient(token) as client:
            await teams_ops.amark_chat_read(client, chat_id, claims["oid"], claims["tid"])
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {"ok": False, "error": "teams_unavailable"}

    return {"ok": True}


def _desktop_attachments(raw: str) -> tuple[list, str]:
    """Turn the desktop's attachments JSON into files to send, or say what is wrong.

    Returns ``(files, reason)``; a non-empty reason means nothing should be
    sent. The bytes arrive base64-encoded because the server has no file
    system the desktop can hand it a path into.
    """
    import json

    if not raw.strip():
        return [], ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [], "attachments must be a JSON array"
    if not isinstance(parsed, list):
        return [], "attachments must be a JSON array"

    resolved = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            return [], f"attachments[{index}]: not an object"
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return [], f"attachments[{index}]: missing name"
        encoded = entry.get("content_base64")
        if not isinstance(encoded, str):
            return [], f"attachments[{index}]: missing content_base64"
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return [], f"attachments[{index}]: invalid base64"
        if not data:
            return [], f"attachments[{index}]: invalid base64"
        content_type = entry.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            return [], f"attachments[{index}]: content_type must be a string"
        resolved.append(
            attachment_ops.ResolvedAttachment(
                name=name.strip(),
                data=data,
                content_type=(content_type or "").strip()
                or attachment_ops.guess_content_type(name),
            )
        )
    return resolved, ""


@mcp.tool()
async def send_chat_message_json(chat_id: str, text: str, attachments: str = "") -> dict:
    """
    Send a plain-text message, optionally with files, to a Teams chat. Returns JSON.

    The content type is explicitly "text", not "auto": the desktop composer is
    a plain-text field, and auto-detection would read a typed "<" as markup —
    the same rule update_draft_body holds for mail. Returns the created message
    flattened exactly as list_chat_messages_page returns one, so the client can
    store its own reply without waiting for the next pull. Requires
    Chat.ReadWrite.

    attachments: every file, inline image, card, or quoted-message reference on
    the message as {id, kind, name, content_type, content_url, thumbnail_url,
    card_text}; kind is one of file, image, card, message_reference, other;
    empty when none. Fetch a file's or image's bytes with
    get_chat_attachment_json. body_content is still the raw Graph body (the
    client owns stripping), so a file-only message has an empty or tag-only
    body — render the attachments list.

    Teams cannot carry file bytes on a message, so each attachment is uploaded
    to the sender's OneDrive "Microsoft Teams Chat Files" folder, shared
    read-only with the chat's members, and the message posts a card pointing at
    it. That upload needs the Files.ReadWrite permission. The created message
    comes back with the file in its attachments list, kind "file". Inline
    images are not supported here yet; every entry is sent as a file.

    On failure message is null and error says why.

    Args:
        chat_id: The chat ID (from list_chats_page).
        text: The message body, sent as typed. May be empty when attachments
            carry the message.
        attachments: JSON array of files, e.g.
            [{"name": "notes.txt", "content_base64": "...", "content_type": "text/plain"}].
            content_type is optional and guessed from the name when absent.
            Empty string sends no files.

    Returns:
        message — the created message, flattened as above.

        Permanent errors, which must not be retried: invalid_attachments (with
        reason, e.g. "attachments[1]: invalid base64"), files_scope_missing (the
        connection cannot write to OneDrive, so no file can be sent),
        teams_unavailable (the account has no Teams license), not_connected
        (with connect_url when one exists).
    """
    if not chat_id.strip():
        return {"message": None, "error": "chat_id must not be empty"}
    sent_files, reason = _desktop_attachments(attachments)
    if reason:
        return {"message": None, "error": "invalid_attachments", "reason": reason}
    if not text.strip() and not sent_files:
        return {"message": None, "error": "text must not be empty"}

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            if sent_files:
                created = await teams_ops.asend_message_with_files(
                    client,
                    content=text,
                    content_type="text",
                    files=sent_files,
                    chat_id=chat_id,
                    exclude_user_id=teams_ops.decode_token_claims(token).get("oid", ""),
                )
            else:
                created = await teams_ops.asend_chat_message(
                    client, chat_id, text, content_type="text"
                )
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {"message": None, "error": "teams_unavailable"}
    except FilesScopeMissingError:
        return {"message": None, "error": "files_scope_missing"}

    return {"message": _chat_message_json(created)}


def _drive_item_json(item: dict) -> dict:
    """Flatten a driveItem for programmatic clients."""
    size = item.get("size")
    file_facet = item.get("file") or {}
    return {
        "item_id": item.get("id"),
        "name": item.get("name"),
        "size": size if isinstance(size, int) else 0,
        "content_type": file_facet.get("mimeType") if isinstance(file_facet, dict) else None,
        "web_url": item.get("webUrl"),
        "modified": item.get("lastModifiedDateTime"),
        "is_folder": "folder" in item,
    }


@mcp.tool()
async def inspect_file_json(
    item_id: str = "", url: str = "", read_content: str = "false", site_id: str = ""
) -> dict:
    """
    Get a file's metadata, and optionally its text, as structured JSON.

    The JSON sibling of inspect_file, for programmatic clients. This is how a
    client resolves a Teams attachment's content_url or a mail link
    attachment's source_url into something it can show or read.

    Accepts either a drive item id (from list_files) or a SharePoint/OneDrive
    sharing URL. An item_id that looks like a sharing URL is treated as one,
    the same way inspect_file does.

    Returns item_id, name, size, content_type, web_url, modified, and
    is_folder. With read_content "true" it also returns text — extracted from
    Word, PowerPoint, Excel, and PDF documents, or decoded for text files, and
    null when the content is binary, a folder, or over the size limits.

    Permanent errors: missing_target (neither an id nor a url was given), and
    for sharing URLs access_denied (403), not_found (404), and invalid_link
    (400); plus not_connected. Everything else, including a 404 for an unknown
    item id, propagates as a tool error.

    Args:
        item_id: A drive item ID (from list_files), or a sharing URL.
        url: A SharePoint/OneDrive sharing URL. Wins over item_id.
        read_content: "true" to also download and return the text.
        site_id: SharePoint site ID. Leave empty for OneDrive. Ignored for URLs.
    """
    sharing_url = url.strip()
    if not sharing_url and item_id and files_ops.is_sharing_url(item_id):
        sharing_url = item_id.strip()
    if not sharing_url and not item_id.strip():
        return {"error": "missing_target"}

    read = read_content.strip().lower() in ("true", "1", "yes")

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            if sharing_url:
                try:
                    if read:
                        item, content = await files_ops.aresolve_sharing_link_content(
                            client, sharing_url
                        )
                        if content is None:
                            (
                                item,
                                content,
                            ) = await files_ops.aresolve_sharing_link_extracted_content(
                                client, sharing_url, item=item
                            )
                    else:
                        item = await files_ops.aresolve_sharing_link(client, sharing_url)
                        content = None
                except GraphError as e:
                    if e.status_code == 403:
                        return {"error": "access_denied"}
                    if e.status_code == 404:
                        return {"error": "not_found"}
                    if e.status_code == 400:
                        return {"error": "invalid_link"}
                    raise
            else:
                if read:
                    item, content = await files_ops.aget_drive_item_content(
                        client, item_id, site_id=site_id
                    )
                    if content is None:
                        item, content = await files_ops.aget_drive_item_extracted_content(
                            client, item_id, site_id=site_id, item=item
                        )
                else:
                    item = await files_ops.aget_drive_item(client, item_id, site_id=site_id)
                    content = None
    except PermissionError as e:
        return _not_connected(e)

    result = _drive_item_json(item)
    if read:
        result["text"] = content
    return result


@mcp.tool()
async def connection_status() -> dict:
    """
    Report whether Microsoft is connected, and with which scopes.

    For programmatic clients deciding what to show before any real call.
    Returns connected, scopes (bare lowercased names, e.g. "mail.read"),
    connect_url (set only when disconnected), and account (the same shape as
    get_profile_json, or null if the profile could not be fetched).

    An empty scopes list on a connected account means "unknown", not "none":
    token rows persisted before the scopes key was corrected have no scopes
    recorded. Clients should read empty as "assume mail is granted, chat is
    not" rather than as a hard denial.

    A connected account also reports mail_policy: {"enabled": bool} — whether
    the mail sender policy is hiding external-sender mail. The allowed domains
    themselves are not published. Use it to explain missing mail, and resync
    the mail cache when the flag flips.
    """
    try:
        token = get_graph_token()
    except PermissionError as e:
        return {
            "connected": False,
            "scopes": [],
            "connect_url": getattr(e, "connect_url", None),
            "account": None,
        }
    except Exception:
        # A status probe must never surface a tool error — clients call it
        # precisely to decide what to show. Anything the auth chain throws
        # beyond the not-connected contract (e.g. MSAL's confidential-client
        # path rejecting a device flow on a stale cache) reads as
        # "not connected, no connect step known".
        logger.warning("connection_status: token acquisition failed", exc_info=True)
        return {"connected": False, "scopes": [], "connect_url": None, "account": None}

    account = None
    try:
        async with AsyncGraphClient(token) as client:
            account = _profile_json(await mail_ops.aget_profile(client))
    except Exception:
        logger.debug("connection_status could not fetch the profile", exc_info=True)

    # A status probe must never crash: a malformed allowlist is reported as
    # "on, but misconfigured" rather than raised, because the tools that would
    # actually read mail already fail closed on it.
    try:
        policy = {"enabled": mail_policy.enabled()}
    except mail_policy.MailPolicyConfigError:
        logger.warning("connection_status: mail sender policy is misconfigured", exc_info=True)
        policy = {"enabled": True, "error": "invalid_config"}

    return {
        "connected": True,
        "scopes": _stored_graph_scopes(),
        "connect_url": None,
        "account": account,
        "mail_policy": policy,
    }


if __name__ == "__main__":
    mcp.run()
