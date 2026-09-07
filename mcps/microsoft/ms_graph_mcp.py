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

Tool summary (40 tools):
  Email     : get_profile, list_emails, sync_mail, read_email*, get_mail_attachment,
              send_email, mark_mail_read, manage_inbox_rules, manage_mail_folders
  Calendar  : list_calendar_events, get_calendar_event, create_calendar_event, check_availability
  Teams     : list_teams, list_chats, read_teams_messages, search_teams_messages,
              get_teams_attachment, send_teams_message, get_teams_activity,
              get_chat_members, ensure_chat, mark_chat_read
  Files     : list_sharepoint_sites, list_files, inspect_file, edit_document, manage_file
  Power BI  : list_powerbi, query_dataset, refresh_dataset, export_report
  Directory : search_people
  Desktop JSON : get_mail_detail, create_reply_draft_json,
                 create_draft_json, update_draft_body, add_draft_attachment_json, send_draft,
                 connection_status

The 1 tool marked ``*`` returns a prose string an LLM reads directly; the
other 39 return a ``dict`` and declare ``output_schema=None``, which opts them into the
FormatNegotiation middleware: a caller sending ``X-Bond-Client: desktop`` (the
desktop mail app) gets the dict as structuredContent, while every other caller
gets a compact text rendering of the same dict. Parameters stay ``str``/``int``
only (empty string = absent) for Bedrock compatibility, as everywhere else in
this server.

Tools that have been renamed keep their old names as deprecated aliases: still
callable, but hidden from tools/list by HideDeprecatedAliases.
"""

import base64
import binascii
import html as html_mod
import json
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from bond_common import (
    DEPRECATED_ALIAS_TAG,
    FormatNegotiation,
    HideDeprecatedAliases,
    render_compact,
)
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

# Per-tool compact renderers, registered beside the tools they serve. Passed
# by reference into the middleware below, so a later registration still lands.
RENDER_OVERRIDES: dict = {}

# Dict-returning tools declare `output_schema=None` so FormatNegotiation may
# drop structuredContent on the compact path — the MCP spec requires structured
# results from any tool that advertises an output schema, and that declaration
# is also how the middleware tells the dict tools from the markdown ones.
mcp.add_middleware(FormatNegotiation(overrides=RENDER_OVERRIDES))
mcp.add_middleware(HideDeprecatedAliases())

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
# Email
# ---------------------------------------------------------------------------


@mcp.tool(output_schema=None)
async def list_emails(
    folder: str = "inbox", query: str = "", top: int = 1000, mailbox: str = "", options: str = ""
) -> dict:
    """
    List recent emails or search email messages.

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
        messages (a row per message), count, folder, query, marked_read, notice.
        Each row carries date, from_name, from_address, to, subject, is_read,
        body_preview, has_attachments, id. The has_attachments column flags
        messages that carry attachments; read_email lists them and
        get_mail_attachment reads one.

        You see this as pipe-CSV — one header line of those column names, one
        line per message — followed by `count:`, `folder:`, `query:`,
        `marked_read:` and `notice:` lines (empty ones are dropped). No messages
        renders as `messages: (none)`.

        While the mail sender policy is on, messages from senders outside the
        allowed domains are omitted and the notice says so; the notice is
        constant, because a hidden count would turn a $search query into a
        content oracle.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    _SELECT = (
        "id,subject,from,sender,isDraft,toRecipients,receivedDateTime,isRead,"
        "hasAttachments,bodyPreview"
    )

    # Resolved before any Graph call so a malformed allowlist fails the call
    # instead of returning unfiltered mail.
    notice = mail_policy.POLICY_NOTICE if mail_policy.enabled() else ""

    mb = mailbox or None
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            # A custom folder display name (anything other than the default inbox) is
            # resolved to a real folder ID; well-known names pass through unchanged.
            resolved_folder: str | None = None
            if folder and folder != "inbox":
                try:
                    resolved_folder = await folder_ops.aresolve_folder_id(
                        client, folder, mailbox=mb
                    )
                except folder_ops.FolderNotFoundError as e:
                    return {"error": "folder_not_found", "reason": str(e)}

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
                    return {
                        "error": "invalid_options",
                        "reason": "Option 'mark_as_read' must be a JSON array of message IDs.",
                    }
                for mid in mark_ids:
                    await mail_ops.amark_read(client, mid, mailbox=mb)
    except PermissionError as e:
        return _not_connected(e)

    rows = []
    for msg in messages:
        sender = msg.get("from", {}).get("emailAddress", {})
        rows.append(
            {
                "date": msg.get("receivedDateTime", ""),
                "from_name": sender.get("name", ""),
                "from_address": sender.get("address", ""),
                "to": ", ".join(
                    r.get("emailAddress", {}).get("address", "")
                    for r in msg.get("toRecipients", [])
                ),
                "subject": msg.get("subject", ""),
                "is_read": msg.get("isRead"),
                "body_preview": msg.get("bodyPreview", ""),
                "has_attachments": msg.get("hasAttachments"),
                "id": msg.get("id", ""),
            }
        )

    return {
        "messages": rows,
        "count": len(rows),
        "folder": folder,
        "query": query,
        "marked_read": len(mark_ids) if isinstance(mark_ids, list) else 0,
        "notice": notice,
    }


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
    names, types, sizes, and IDs. Pass an ID to get_mail_attachment to read,
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


@mcp.tool(output_schema=None)
async def get_mail_attachment(
    message_id: str, attachment_id: str, mode: str = "text", mailbox: str = "", options: str = ""
) -> dict:
    """
    Read, download, or save one attachment from an email message.

    Get the IDs from read_email, which lists a message's attachments.

    Args:
        message_id: The Graph API message ID (from list_emails output).
        attachment_id: The attachment ID (from read_email's attachment list).
        mode: What to return.
            "text" (default) — the extracted text: Word, PowerPoint, Excel, and
                PDF documents are parsed, plain-text files are decoded, and
                binaries say why there is no text instead.
            "metadata" — the summary only; no content is fetched.
            "bytes" — the raw bytes, base64-encoded, up to 10 MB.
            "onedrive" — save a copy to your OneDrive and return the link.
        mailbox: Shared mailbox email address (e.g. "support@company.com"). Leave empty
            to access your own mailbox. Requires Mail.Read.Shared permission and Exchange
            Full Access delegation on the shared mailbox.
        options: JSON string with optional fields:
            {"folder_path": "Attachments"}  — OneDrive folder for mode "onedrive"
                (default "Attachments"; created if missing).
            {"site_id": ""}  — save to this SharePoint site's drive instead of OneDrive.

    Returns:
        Every mode returns the attachment summary — id, name, content_type,
        size (bytes), is_inline, content_id, kind (file | item | reference |
        unknown), and source_url for a link attachment — plus what the mode
        adds. An attached message (kind "item") also carries item_subject,
        item_from, and item_received in metadata and text modes, and downloads
        as .eml bytes in the others.

        "text" adds text, truncated, and — when there is no text — reason
        ("binary" | "unsupported" | "too_large" | "reference"). "bytes" adds
        content_base64. "onedrive" adds item_id and web_url, and its name,
        content_type, and size describe the file as saved.

        Permanent errors, which must not be retried: external_sender (the mail
        sender policy hides the carrying message, or the message attached to
        it), invalid_mode, invalid_options, reference (with source_url — a link
        attachment has no bytes; open the URL with inspect_file), too_large
        (with size and limit; bytes mode only, decided from the metadata so
        nothing is downloaded), not_connected (with connect_url when one
        exists). Everything else — a Graph 404 for an unknown message or
        attachment, throttling, 5xx — propagates as a tool error, which is the
        caller's "transient, retry later" signal.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    mode = mode.strip().lower() or "text"
    if mode not in ("metadata", "text", "bytes", "onedrive"):
        return {
            "error": "invalid_mode",
            "reason": f"mode must be one of: metadata, text, bytes, onedrive; got {mode!r}",
        }

    mb = mailbox or None
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            # Judge the parent message before any of its attachment metadata or
            # bytes are read, against the same mailbox the reads will use.
            # Inside the try so a malformed policy allowlist propagates as a
            # tool error rather than being mistaken for a missing connection.
            if not await mail_policy.acheck_message(client, message_id, mb):
                return {"error": mail_policy.EXTERNAL_SENDER_ERROR}

            meta = await attachment_ops.aget_attachment_metadata(
                client, message_id, attachment_id, mailbox=mb
            )
            summary = attachment_ops.attachment_summary(meta)

            # An attached message is judged by the same rule as a message;
            # fetched once here and handed to whichever mode needs it. An
            # attached event or contact has no sender and is therefore hidden.
            expanded: dict | None = None
            if summary["kind"] == "item" and mail_policy.enabled():
                expanded = await attachment_ops.aget_item_attachment(
                    client, message_id, attachment_id, mailbox=mb
                )
                if not mail_policy.message_allowed(expanded.get("item") or {}):
                    return {"error": mail_policy.EXTERNAL_SENDER_ERROR}

            if mode == "metadata":
                if summary["kind"] != "item":
                    return summary
                if expanded is None:
                    expanded = await attachment_ops.aget_item_attachment(
                        client, message_id, attachment_id, mailbox=mb
                    )
                return {**summary, **_attachment_item_fields(expanded.get("item") or {})}

            if mode == "text":
                return await _attachment_json_text(
                    client, message_id, attachment_id, summary, expanded, mailbox=mb
                )

            if summary["kind"] == "reference":
                return {"error": "reference", "source_url": summary["source_url"]}

            if mode == "onedrive":
                # No size ceiling here: aupload_any switches to an upload
                # session for anything the simple PUT cannot carry.
                data, header_type = await attachment_ops.aget_attachment_bytes(
                    client, message_id, attachment_id, mailbox=mb
                )
                name = summary["name"] or attachment_id
                if summary["kind"] == "item":
                    # An attached message's bytes are a MIME message, so it is
                    # saved as the .eml a mail client can open.
                    att = attachment_ops.ResolvedAttachment(
                        name=name if name.lower().endswith(".eml") else f"{name}.eml",
                        data=data,
                        content_type="message/rfc822",
                    )
                else:
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
                    "onedrive",
                    folder_path=opt_str(opts.get("folder_path")) or "Attachments",
                    site_id=opt_str(opts.get("site_id")) or "",
                )
                # The delivered name, type, and size win over the record's: an
                # attached message is saved under a name the record never had.
                return {
                    **summary,
                    "name": result["name"],
                    "content_type": result["content_type"],
                    "size": result["size"],
                    "item_id": result["item_id"],
                    "web_url": result["web_url"],
                }

            # Decided from the metadata size, so an oversized attachment is
            # refused before its bytes cross the wire.
            if summary["size"] > attachment_ops.MAX_JSON_ATTACHMENT_BYTES:
                return {
                    "error": "too_large",
                    "size": summary["size"],
                    "limit": attachment_ops.MAX_JSON_ATTACHMENT_BYTES,
                }
            data, header_type = await attachment_ops.aget_attachment_bytes(
                client, message_id, attachment_id, mailbox=mb
            )
    except PermissionError as e:
        return _not_connected(e)

    if not summary["content_type"]:
        fallback = "message/rfc822" if summary["kind"] == "item" else ""
        summary["content_type"] = header_type or fallback
    return {**summary, "content_base64": base64.b64encode(data).decode("ascii")}


@mcp.tool(output_schema=None)
async def send_email(
    to: str, subject: str, body: str, mailbox: str = "", options: str = ""
) -> dict:
    """
    Send an email message.

    Graph accepts the send asynchronously, so a successful return means
    "queued", not "delivered".

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

    Returns:
        ok, plus the sent mail's id, conversation_id, internet_message_id,
        subject, to, cc, bcc_count, from, attachments, and sent_at. The id is
        the draft the send was built from and stops resolving once the copy
        lands in Sent Items, so it serves only as a client-side key;
        internet_message_id and conversation_id carry over to the sent copy, so
        the Sent Items copy is found by matching internet_message_id. bcc_count
        stands in for the BCC addresses, which are deliberately not echoed back.
        sent_at is the server's UTC clock when Graph queued the send; Exchange's
        own sentDateTime may differ from it by seconds.

        You see this as one `key: value` line per field, empty fields included.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}
    body_type = opts.get("body_type", "auto")
    cc = opts.get("cc", "")
    bcc = opts.get("bcc", "")
    from_address = opts.get("from_address", "")

    mb = mailbox or None
    to_list = [addr.strip() for addr in to.split(",") if addr.strip()]
    cc_list = [addr.strip() for addr in cc.split(",") if addr.strip()] if cc else None
    bcc_list = [addr.strip() for addr in bcc.split(",") if addr.strip()] if bcc else None

    resolved: list = []
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            specs = opts.get("attachments")
            if specs is not None:
                try:
                    resolved = await attachment_ops.aresolve_attachment_sources(client, specs)
                except ValueError as e:
                    return {"error": "invalid_attachments", "reason": str(e)}
            draft = await mail_ops.asend_message(
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
    except PermissionError as e:
        return _not_connected(e)

    return {
        "ok": True,
        "id": draft["id"],
        "conversation_id": draft.get("conversationId"),
        "internet_message_id": draft.get("internetMessageId"),
        "subject": subject,
        "to": ", ".join(to_list),
        "cc": ", ".join(cc_list) if cc_list else "",
        "bcc_count": len(bcc_list) if bcc_list else 0,
        "from": mailbox or from_address or "",
        "attachments": ", ".join(f"{a.name} ({_format_size(len(a.data))})" for a in resolved),
        "sent_at": _utcnow_iso(),
    }


def _summarize_rule_keys(predicate: dict) -> str:
    """Summarize a rule's conditions/actions as a comma-joined list of their keys."""
    return ", ".join(predicate.keys()) if isinstance(predicate, dict) else ""


@mcp.tool(output_schema=None)
async def manage_inbox_rules(action: str = "list", rule_id: str = "", options: str = "") -> dict:
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
        list: rules (id, display_name, sequence, is_enabled, conditions,
            actions) and count — conditions/actions are the comma-joined key
            names, not their values. You see pipe-CSV of those columns plus a
            trailing `count:` line; no rules renders as `rules: (none)`.
        get: rule — the whole Graph rule object, conditions and actions with
            their values. You see it as compact JSON.
        create/update: action ("created"/"updated"), id, display_name, as
            `key: value` lines. While the mail sender policy is on, a rule that
            forwards or redirects mail is refused, because it would re-deliver
            external mail as internal.
        delete: action ("deleted") and id.
    """
    action = action.strip().lower()
    valid_actions = {"list", "get", "create", "update", "delete"}
    if action not in valid_actions:
        return {
            "error": "invalid_action",
            "reason": f"Unknown action {action!r}. Use one of: {', '.join(sorted(valid_actions))}.",
        }

    if action in ("get", "update", "delete") and not rule_id:
        return {
            "error": "missing_rule_id",
            "reason": f"A rule_id is required for action {action!r}.",
        }

    if action in ("create", "update"):
        opts, err = parse_options(options)
        if err:
            return {"error": "invalid_options", "reason": err}
        if not isinstance(opts, dict) or not opts:
            return {
                "error": "invalid_options",
                "reason": (
                    f"Action {action!r} requires a non-empty options JSON object "
                    "(the rule definition)."
                ),
            }
        if action == "create":
            missing = {"displayName", "sequence", "actions"} - opts.keys()
            if missing:
                return {
                    "error": "invalid_options",
                    "reason": f"Action 'create' requires: {', '.join(sorted(missing))}.",
                }

        # Refused before the token is even acquired: a forwarding rule
        # re-originates every external message as an internal one, which is a
        # durable, self-service bypass of the whole policy.
        if mail_policy.enabled() and mail_policy.rule_forwards(opts):
            return {"error": "forwarding_rule", "reason": mail_policy.FORWARDING_RULE_TEXT}

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            if action == "list":
                rules = await mail_ops.alist_inbox_rules(client)
                return {
                    "rules": [
                        {
                            "id": rule.get("id", ""),
                            "display_name": rule.get("displayName", ""),
                            "sequence": rule.get("sequence"),
                            "is_enabled": rule.get("isEnabled"),
                            "conditions": _summarize_rule_keys(rule.get("conditions", {})),
                            "actions": _summarize_rule_keys(rule.get("actions", {})),
                        }
                        for rule in rules
                    ],
                    "count": len(rules),
                }

            if action == "get":
                return {"rule": await mail_ops.aget_inbox_rule(client, rule_id)}

            if action == "create":
                created = await mail_ops.acreate_inbox_rule(client, opts) or {}
                return {
                    "action": "created",
                    "id": created.get("id"),
                    "display_name": created.get("displayName"),
                }

            if action == "update":
                updated = await mail_ops.aupdate_inbox_rule(client, rule_id, opts) or {}
                return {
                    "action": "updated",
                    "id": updated.get("id", rule_id),
                    "display_name": updated.get("displayName"),
                }

            await mail_ops.adelete_inbox_rule(client, rule_id)
            return {"action": "deleted", "id": rule_id}
    except PermissionError as e:
        return _not_connected(e)


# ---------------------------------------------------------------------------
# Mail folders
# ---------------------------------------------------------------------------


@mcp.tool(output_schema=None)
async def manage_mail_folders(action: str = "list", folder_id: str = "", options: str = "") -> dict:
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
        list: folders (id, display_name, child_folder_count, total_item_count,
            unread_item_count) and count. You see pipe-CSV of those columns plus
            a trailing `count:` line; no folders renders as `folders: (none)`.
        get: folder — the whole Graph folder object, including its ID for use in
            other calls. You see it as compact JSON.
        create/rename: action ("created"/"renamed"), id, display_name, as
            `key: value` lines.
        move: action ("moved"), id, parent_id.
        delete: action ("deleted") and id.
    """
    action = action.strip().lower()
    valid_actions = {"list", "get", "create", "rename", "move", "delete"}
    if action not in valid_actions:
        return {
            "error": "invalid_action",
            "reason": f"Unknown action {action!r}. Use one of: {', '.join(sorted(valid_actions))}.",
        }

    if action in ("get", "rename", "move", "delete") and not folder_id:
        return {
            "error": "missing_folder_id",
            "reason": f"A folder_id is required for action {action!r}.",
        }

    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    if action in ("create", "rename"):
        display_name = str(opts.get("display_name", "")).strip()
        if not display_name:
            return {
                "error": "invalid_options",
                "reason": f"Action {action!r} requires a non-empty 'display_name' in options.",
            }

    if action == "move":
        destination_id = str(opts.get("destination_id", "")).strip()
        if not destination_id:
            return {
                "error": "invalid_options",
                "reason": "Action 'move' requires a non-empty 'destination_id' in options.",
            }

    try:
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
                return {
                    "folders": [
                        {
                            "id": folder.get("id", ""),
                            "display_name": folder.get("displayName", ""),
                            "child_folder_count": folder.get("childFolderCount"),
                            "total_item_count": folder.get("totalItemCount"),
                            "unread_item_count": folder.get("unreadItemCount"),
                        }
                        for folder in result
                    ],
                    "count": len(result),
                }

            if action == "get":
                return {"folder": await folder_ops.aget_folder(client, folder_id)}

            if action == "create":
                parent_id = opt_str(opts.get("parent_id"))
                created = (
                    await folder_ops.acreate_folder(client, display_name, parent_id=parent_id)
                ) or {}
                return {
                    "action": "created",
                    "id": created.get("id"),
                    "display_name": created.get("displayName"),
                }

            if action == "rename":
                updated = (await folder_ops.arename_folder(client, folder_id, display_name)) or {}
                return {
                    "action": "renamed",
                    "id": updated.get("id", folder_id),
                    "display_name": updated.get("displayName"),
                }

            if action == "move":
                moved = (await folder_ops.amove_folder(client, folder_id, destination_id)) or {}
                return {
                    "action": "moved",
                    "id": folder_id,
                    "parent_id": moved.get("parentFolderId", destination_id),
                }

            await folder_ops.adelete_folder(client, folder_id)
            return {"action": "deleted", "id": folder_id}
    except PermissionError as e:
        return _not_connected(e)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@mcp.tool(output_schema=None)
async def list_calendar_events(
    start_date: str = "",
    end_date: str = "",
    top: int = 10,
) -> dict:
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

    Returns:
        events (a row per event) and count. Each row carries subject, start,
        end, timezone, organizer, location, online_url, is_all_day,
        is_cancelled, id — start/end are the raw Graph dateTime strings and
        timezone applies to both.

        You see this as pipe-CSV of those columns plus a trailing `count:` line;
        an empty range renders as `events: (none)`.
    """
    from datetime import datetime, timedelta
    from datetime import timezone as tz

    if not start_date:
        now = datetime.now(tz.utc)
        start_date = now.isoformat()
    if not end_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except ValueError as e:
            return {"error": "invalid_date", "reason": str(e)}
        end_date = (start_dt + timedelta(days=7)).isoformat()

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            events = await calendar_ops.alist_calendar_events(
                client, start_datetime=start_date, end_datetime=end_date, top=top
            )
    except PermissionError as e:
        return _not_connected(e)

    rows = []
    for event in events:
        start = event.get("start", {})
        rows.append(
            {
                "subject": event.get("subject", ""),
                "start": start.get("dateTime", ""),
                "end": event.get("end", {}).get("dateTime", ""),
                "timezone": start.get("timeZone", ""),
                "organizer": event.get("organizer", {}).get("emailAddress", {}).get("name", ""),
                "location": event.get("location", {}).get("displayName", ""),
                "online_url": event.get("onlineMeetingUrl", ""),
                "is_all_day": event.get("isAllDay"),
                "is_cancelled": event.get("isCancelled"),
                "id": event.get("id", ""),
            }
        )

    return {"events": rows, "count": len(rows)}


@mcp.tool(output_schema=None)
async def get_calendar_event(event_id: str, options: str = "") -> dict:
    """
    Get detailed information about a specific calendar event.

    Args:
        event_id: The event ID (from list_calendar_events output).
        options: JSON string with optional fields:
            {"max_content_length": -1}  — max characters for the event body. Default -1
                (no limit). Set a positive integer to truncate long event descriptions.

    Returns:
        attendees (a row per attendee: name, address, response), plus subject,
        start, end, timezone, organizer_name, organizer_address, location,
        online_url, is_all_day, recurrence, id, body_type, body_text.
        start/end are the raw Graph dateTime strings and timezone applies to
        both; body_type ("html" or "text") says how to read body_text.

        You see this as pipe-CSV of the attendee columns followed by one
        `key: value` line per remaining field, with the empty ones dropped; an
        event nobody was invited to renders as `attendees: (none)`.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    max_content_length = opt_int(opts.get("max_content_length"), -1)

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            event = await calendar_ops.aget_calendar_event(client, event_id)
    except PermissionError as e:
        return _not_connected(e)

    start = event.get("start", {})
    organizer = event.get("organizer", {}).get("emailAddress", {})
    body = event.get("body", {})
    body_text = body.get("content", "")
    if max_content_length > 0:
        body_text = body_text[:max_content_length]

    recurrence = event.get("recurrence")
    pattern = (recurrence or {}).get("pattern", {})

    return {
        "attendees": [
            {
                "name": att.get("emailAddress", {}).get("name", ""),
                "address": att.get("emailAddress", {}).get("address", ""),
                "response": att.get("status", {}).get("response", "none"),
            }
            for att in event.get("attendees", [])
        ],
        "subject": event.get("subject", ""),
        "start": start.get("dateTime", ""),
        "end": event.get("end", {}).get("dateTime", ""),
        "timezone": start.get("timeZone", ""),
        "organizer_name": organizer.get("name", ""),
        "organizer_address": organizer.get("address", ""),
        "location": event.get("location", {}).get("displayName", ""),
        "online_url": event.get("onlineMeetingUrl", ""),
        "is_all_day": event.get("isAllDay"),
        "recurrence": (
            f"{pattern.get('type', 'unknown')} (every {pattern.get('interval', 1)})"
            if recurrence
            else ""
        ),
        "id": event.get("id", ""),
        "body_type": body.get("contentType", ""),
        "body_text": body_text,
    }


@mcp.tool(output_schema=None)
async def create_calendar_event(
    subject: str,
    start_datetime: str,
    end_datetime: str,
    timezone: str = "UTC",
    options: str = "",
) -> dict:
    """
    Create a new calendar event.

    Args:
        subject: Event title/subject.
        start_datetime: Start date and time in ISO 8601 format (e.g., "2026-05-08T10:00:00").
        end_datetime: End date and time in ISO 8601 format (e.g., "2026-05-08T11:00:00").
        timezone: IANA timezone for start/end times (e.g., "America/New_York", "UTC"). Default: UTC.
        options: JSON string with optional fields:
            {"attendees": "a@b.com,c@d.com", "location": "Room 42", "body": "Meeting notes...", "is_online_meeting": true, "is_all_day": false}

    Returns:
        ok, id, subject, start, end, timezone, online_meeting_url, web_link —
        start/end/timezone come back from Graph, which may normalize what was
        asked for. You see one `key: value` line per field.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}
    attendees = opts.get("attendees", "")
    location = opts.get("location", "")
    body = opts.get("body", "")
    is_online_meeting = opt_bool(opts.get("is_online_meeting"), False)
    is_all_day = opt_bool(opts.get("is_all_day"), False)

    attendee_list = (
        [addr.strip() for addr in attendees.split(",") if addr.strip()] if attendees else None
    )

    try:
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
    except PermissionError as e:
        return _not_connected(e)

    start = event.get("start", {})
    return {
        "ok": True,
        "id": event.get("id", ""),
        "subject": subject,
        "start": start.get("dateTime", ""),
        "end": event.get("end", {}).get("dateTime", ""),
        "timezone": start.get("timeZone", ""),
        "online_meeting_url": event.get("onlineMeetingUrl", ""),
        "web_link": event.get("webLink", ""),
    }


@mcp.tool(output_schema=None)
async def check_availability(
    emails: str,
    start_datetime: str,
    end_datetime: str,
    timezone: str = "UTC",
) -> dict:
    """
    Check free/busy availability for one or more people.

    Useful for finding meeting times. Returns availability status for each
    person in the specified time range.

    Args:
        emails: Comma-separated email addresses to check availability for.
        start_datetime: Start of the time range in ISO 8601 format (e.g., "2026-05-08T09:00:00").
        end_datetime: End of the time range in ISO 8601 format (e.g., "2026-05-08T17:00:00").
        timezone: IANA timezone (e.g., "America/New_York", "UTC"). Default: UTC.

    Returns:
        busy (a row per busy block across everyone asked about: person, start,
        end, subject, status — subject is "(private)" when the calendar does not
        share it), summary (one free-percentage phrase per person, joined with
        "; "), and busy_count. Every block is listed, so busy_count is the real
        total.

        You see this as pipe-CSV of the busy columns plus trailing `summary:`
        and `busy_count:` lines; nobody busy renders as `busy: (none)`.
    """
    email_list = [addr.strip() for addr in emails.split(",") if addr.strip()]
    if not email_list:
        return {"error": "invalid_arguments", "reason": "No email addresses provided."}

    try:
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
    except PermissionError as e:
        return _not_connected(e)

    schedules = result.get("value", [])
    if not schedules:
        return {"error": "no_data", "reason": "No availability information returned."}

    busy = []
    summaries = []
    for sched in schedules:
        person = sched.get("scheduleId", "")
        avail_view = sched.get("availabilityView", "")
        free_count = avail_view.count("0")
        total_slots = len(avail_view)
        if total_slots:
            free_pct = int((free_count / total_slots) * 100)
            summaries.append(f"{person}: {free_pct}% free ({free_count}/{total_slots} slots)")
        else:
            summaries.append(f"{person}: no slots")

        for item in sched.get("scheduleItems", []):
            busy.append(
                {
                    "person": person,
                    "start": item.get("start", {}).get("dateTime", ""),
                    "end": item.get("end", {}).get("dateTime", ""),
                    "subject": item.get("subject", "(private)"),
                    "status": item.get("status", ""),
                }
            )

    return {"busy": busy, "summary": "; ".join(summaries), "busy_count": len(busy)}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


@mcp.tool(output_schema=None)
async def list_teams(team_id: str = "") -> dict:
    """
    List joined Microsoft Teams, or list channels within a specific team.

    When team_id is empty, returns all teams the user has joined.
    When team_id is provided, returns the channels within that team.

    Args:
        team_id: Team ID to list channels for (from a previous call with no team_id).
                 Leave empty to list all joined teams.

    Returns:
        Without team_id: teams (a row per team: name, id) and count.
        With team_id: channels (a row per channel: name, id), count, and
        team_id.

        You see this as pipe-CSV — a `name|id` header line, one line per team or
        channel — followed by `count:` and, in channel mode, `team_id:`. Nothing
        joined renders as `teams: (none)`; a team with no channels renders as
        `channels: (none)`.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            if team_id:
                channels = await teams_ops.alist_channels(client, team_id)
                rows = [
                    {"name": ch.get("displayName", ""), "id": ch.get("id", "")} for ch in channels
                ]
                return {"channels": rows, "count": len(rows), "team_id": team_id}

            team_list = await teams_ops.alist_joined_teams(client)
            rows = [{"name": t.get("displayName", ""), "id": t.get("id", "")} for t in team_list]
            return {"teams": rows, "count": len(rows)}
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {
            "error": "teams_not_available",
            "reason": (
                "Microsoft Teams is not available for this account. "
                "A Microsoft 365 license is required."
            ),
        }


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


def _chat_row(chat: dict) -> dict:
    """One list_chats row. Key order here is the compact CSV's column order.

    Tolerates a chat with no members: cursors the desktop stored before the
    members expansion joined the query still page in without them.
    """
    preview = chat.get("lastMessagePreview") or {}
    names = [m.get("displayName") for m in chat.get("members") or [] if m.get("displayName")]
    members = ", ".join(names[:5])
    if len(names) > 5:
        members += f" (+{len(names) - 5} more)"
    return {
        "unread": _is_chat_unread(chat),
        "chat_type": chat.get("chatType"),
        "topic": chat.get("topic"),
        "members": members or None,
        "last_sender": ((preview.get("from") or {}).get("user") or {}).get("displayName") or None,
        "last_preview": ((preview.get("body") or {}).get("content") or "")[:100] or None,
        "last_preview_at": preview.get("createdDateTime"),
        "last_read_at": (chat.get("viewpoint") or {}).get("lastMessageReadDateTime"),
        "id": chat.get("id"),
    }


@mcp.tool(output_schema=None)
async def list_chats(
    chat_type: str = "", top: int = 50, cursor: str = "", options: str = ""
) -> dict:
    """
    List Teams chats (1:1, group, meeting), newest activity first.

    Args:
        chat_type: Filter by type: oneOnOne, group, or meeting. Empty for all.
            Ignored when cursor is set.
        top: Maximum number of chats to return (default: 50, max: 2000).
            Ignored when cursor is set. When you mean to continue with
            next_cursor, prefer a multiple of 50: Graph pages by 50 and the
            cursor resumes at the next page, so rows trimmed beyond top do
            not reappear.
        cursor: A next_cursor from a previous call, fetched verbatim — the URL
            already encodes the chat_type and page size of the call that
            produced it. Empty starts at page one.
        options: JSON string with optional fields:
            {"mark_as_read": ["chat_id1", "chat_id2"]}  — mark those chat IDs as
                read after listing.

    Returns:
        chats (a row per chat: unread, chat_type, topic, members, last_sender,
        last_preview, last_preview_at, last_read_at, id), count, next_cursor,
        and marked_as_read (how many ids the mark_as_read option acknowledged,
        present only when it was used).

        topic is null for 1:1 chats — name them from members, or resolve the
        full roster with get_chat_members. members holds the first five display
        names joined by ", " with "(+N more)" appended beyond that, and is null
        when Graph sent no member names. last_read_at is how far the signed-in
        user has read the chat, null when Graph sends no viewpoint;
        last_preview_at is null on a chat that never carried a message.
        next_cursor is empty when the listing is complete.

        You see this as pipe-CSV of those nine columns followed by `count:` and
        `next_cursor:` lines (empty ones are dropped). No chats renders as
        `chats: (none)`.

        Permanent errors: invalid_options, invalid_arguments (an unknown
        chat_type), invalid_cursor (a cursor that is not a Graph URL — refused
        without any request), no_identity (the signed-in user cannot be read
        off the token, so nothing can be marked read), teams_unavailable, and
        not_connected.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    if chat_type not in ("", "oneOnOne", "group", "meeting"):
        return {
            "error": "invalid_arguments",
            "reason": (
                f"Invalid chat_type: {chat_type}. "
                "Must be one of: oneOnOne, group, meeting (or empty for all)."
            ),
        }

    mark_ids = opts.get("mark_as_read", [])
    if mark_ids and not isinstance(mark_ids, list):
        return {
            "error": "invalid_options",
            "reason": "Option 'mark_as_read' must be a JSON array of chat IDs.",
        }

    top = min(top, 2000)

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            raw: list[dict] = []
            if cursor:
                data = await teams_ops.achats_page(client, cursor=cursor)
                raw = data.get("value", [])
                next_cursor = data.get("@odata.nextLink", "")
            else:
                # Graph caps /me/chats at $top=50, so a bigger top pages
                # internally; the json ancestor simply errored past 50.
                page_cursor = ""
                next_cursor = ""
                while True:
                    data = await teams_ops.achats_page(
                        client, cursor=page_cursor, top=min(top, 50), chat_type=chat_type
                    )
                    raw.extend(data.get("value", []))
                    next_cursor = data.get("@odata.nextLink", "")
                    if len(raw) >= top or not next_cursor:
                        break
                    page_cursor = next_cursor
                raw = raw[:top]

            if mark_ids:
                claims = teams_ops.decode_token_claims(token)
                if not claims["oid"] or not claims["tid"]:
                    return {"error": "no_identity"}
                for cid in mark_ids:
                    await teams_ops.amark_chat_read(client, cid, claims["oid"], claims["tid"])
    except PermissionError as e:
        return _not_connected(e)
    except NonGraphUrlError:
        logger.warning("list_chats: refused a non-Graph cursor")
        return {"error": "invalid_cursor"}
    except TeamsNotAvailableError:
        return {
            "error": "teams_unavailable",
            "reason": "Microsoft Teams is not available for this account.",
        }

    out = {
        "chats": [_chat_row(chat) for chat in raw],
        "count": len(raw),
        "next_cursor": next_cursor,
    }
    if mark_ids:
        out["marked_as_read"] = len(mark_ids)
    return out


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


def _teams_attachment_not_found(entries: list[dict]) -> dict:
    """No such id — say which ids the message does carry, so a retry can land."""
    return {
        "error": "not_found",
        "available": [
            {"kind": e["kind"], "id": e["id"], "name": e.get("name")} for e in entries if e["id"]
        ],
    }


@mcp.tool(output_schema=None)
async def read_teams_messages(
    team_id: str = "",
    channel_id: str = "",
    chat_id: str = "",
    since: str = "",
    cursor: str = "",
    options: str = "",
) -> dict:
    """
    Read messages from a Teams channel or chat.

    Provide either:
    - chat_id to read from a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to read from a team channel (from list_teams)
    chat_id wins when both are given.

    Two modes:
    - Default: every message back to `since` (last 7 days when `since` is
      empty), paginated internally, filtered on CREATION time.
    - Page mode, entered with options {"page": true} or any cursor: ONE page,
      newest first, where `since` filters LAST-MODIFIED time so an edited
      message resurfaces. This is the incremental-sync mode programmatic
      callers use — compare the last_modified you stored, not created. Empty
      `since` means the newest page of all time. Chats only.

    Args:
        team_id: Team ID (from list_teams with no team_id). Required for channel reading.
        channel_id: Channel ID (from list_teams with team_id). Required for channel reading.
        chat_id: Chat ID (from list_chats). Use this for 1:1 and group chats.
        since: ISO date or datetime cutoff (e.g. '2026-06-16' or '2026-06-16T00:00:00Z').
        cursor: A next_cursor from a previous call; implies page mode. Empty
            starts at page one.
        options: JSON string with optional fields:
            {"page": true}  — one page instead of everything since the cutoff.
            {"mark_as_read": true/false}  — mark the chat as read afterwards.
                Only works with chat_id (channels have no per-user read state).
            {"max_content_length": -1}  — max characters of message body you
                see. Default -1 (no limit); it truncates the rendering only,
                never the message stored under body_content.

    Returns:
        messages (a row per message: id, message_type, from_user_id,
        from_user_display, from_application_id, body_content,
        body_content_type, mentioned_user_ids, created, last_modified,
        attachments), count, next_cursor (empty outside page mode and at the
        end of a listing), and marked_as_read when the option was used.

        System events have no sender, so the from_* fields are null.
        mentioned_user_ids is the Graph user ids the message @mentions, in the
        order they appear and empty when none. body_content is the raw Graph
        body, so a file-only message has an empty or tag-only body.
        attachments carries every file, inline image, card, or quoted-message
        reference as {id, kind, name, content_type, content_url,
        thumbnail_url, card_text}; kind is one of file, image, card,
        message_reference, other.

        You see this as pipe-CSV of timestamp|sender|content|attachments|id
        followed by `count:` and `next_cursor:` lines. HTML bodies are stripped
        to text; an app sender reads `(app)` and a system event `(system)`. The
        attachments column lists shared files as `name [file:<id>]`, inline
        images as `[image:<id>]`, and cards as `[card]`; pass a file or image
        id to get_teams_attachment to read or download it. The content column
        repeats them as `[File: name]` / `[Image]` markers.

        Permanent errors: invalid_options, invalid_arguments (no ids, paging a
        channel, or marking a channel read), invalid_date, invalid_cursor (a
        cursor that is not a Graph URL — refused without any request),
        no_identity, teams_unavailable, and not_connected.
    """
    from datetime import datetime, timedelta, timezone

    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    if not chat_id and not (team_id and channel_id):
        return {
            "error": "invalid_arguments",
            "reason": "Provide either chat_id, or both team_id and channel_id.",
        }

    page = opt_bool(opts.get("page"), False) or bool(cursor)
    if page and not chat_id:
        return {
            "error": "invalid_arguments",
            "reason": "Cursor paging is only supported for chats.",
        }

    if since:
        try:
            since = teams_ops.normalize_since(since)
        except ValueError as e:
            return {"error": "invalid_date", "reason": str(e)}
    elif not page:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    max_content_length = opt_int(opts.get("max_content_length"), -1)
    mark = opts.get("mark_as_read")
    should_mark = mark is not None and opt_bool(mark, True)
    if mark is not None and not chat_id:
        return {
            "error": "invalid_arguments",
            "reason": "Option 'mark_as_read' is only supported for chats, not channels.",
        }

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            next_cursor = ""
            if page:
                data = await teams_ops.achat_messages_page(
                    client, chat_id, since=since, cursor=cursor
                )
                messages = data.get("value", [])
                next_cursor = data.get("@odata.nextLink", "")
            elif chat_id:
                messages = await teams_ops.alist_chat_messages(client, chat_id, since=since)
            else:
                messages = await teams_ops.alist_channel_messages(
                    client, team_id, channel_id, since=since
                )

            if should_mark:
                claims = teams_ops.decode_token_claims(token)
                if not claims["oid"] or not claims["tid"]:
                    return {"error": "no_identity"}
                await teams_ops.amark_chat_read(client, chat_id, claims["oid"], claims["tid"])
    except PermissionError as e:
        return _not_connected(e)
    except NonGraphUrlError:
        logger.warning("read_teams_messages: refused a non-Graph cursor")
        return {"error": "invalid_cursor"}
    except TeamsNotAvailableError:
        return {
            "error": "teams_unavailable",
            "reason": "Microsoft Teams is not available for this account.",
        }

    out = {
        "messages": [_chat_message_json(m) for m in messages],
        "count": len(messages),
        "next_cursor": next_cursor,
    }
    if should_mark:
        out["marked_as_read"] = True
    if max_content_length > 0:
        # A rendering hint the compact renderer consumes and then drops. The
        # desktop passes no options, so it never sees this key.
        out["max_content_length"] = max_content_length
    return out


def _teams_message_text(row: dict, max_len: int) -> str:
    """The content column of one rendered message row.

    Mirrors teams_ops.extract_message_text, which reads a raw Graph message;
    this reads the flattened row the tool has already built.
    """
    content = row["body_content"] or ""
    if row["body_content_type"] == "html" and content:
        content = re.sub(r"<[^>]+>", "", content)
        content = html_mod.unescape(content).replace("\xa0", " ").strip()

    if not content.strip():
        for entry in row["attachments"]:
            if entry["kind"] == "card" and entry["card_text"]:
                content = f"[Card] {entry['card_text']}"
                break

    markers = []
    for entry in row["attachments"]:
        if entry["kind"] == "file":
            markers.append(f"[File: {entry['name'] or '(unnamed)'}]")
        elif entry["kind"] == "image":
            markers.append("[Image]")

    if not content.strip():
        return " ".join(markers) or "(empty)"
    # Markers are appended AFTER truncation: max_content_length caps the body a
    # person wrote, not the evidence that a file came with it.
    if max_len > 0 and len(content) > max_len:
        content = content[:max_len] + "..."
    return f"{content} {' '.join(markers)}" if markers else content


def _render_read_teams_messages(payload: dict) -> str:
    """Flatten the canonical message rows into the pipe-CSV a model reads."""
    if "error" in payload:
        # An override preempts every rule in render_compact, the error envelope
        # included, so an error payload has to be handed back to the shared one.
        return render_compact(payload)

    max_len = payload.get("max_content_length", -1)
    rows = [
        {
            "timestamp": row["created"],
            # The canonical row carries the sending app's id, never its name,
            # so an app is labelled by kind rather than misnamed by its id.
            "sender": row["from_user_display"]
            or ("(app)" if row["from_application_id"] else "(system)"),
            "content": _teams_message_text(row, max_len),
            "attachments": _teams_attachment_column(row["attachments"]),
            "id": row["id"],
        }
        for row in payload["messages"]
    ]
    rest = {k: v for k, v in payload.items() if k not in ("messages", "max_content_length")}
    return render_compact({"messages": rows, **rest})


RENDER_OVERRIDES["read_teams_messages"] = _render_read_teams_messages


@mcp.tool(output_schema=None)
async def search_teams_messages(
    query: str,
    since: str = "",
    conversation_id: str = "",
    options: str = "",
) -> dict:
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
               '2026-01-01T00:00:00Z' (UTC). EMPTY MEANS ALL TIME — unlike
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

    Returns:
        messages (a row per hit: timestamp, sender, conversation, content,
        attachments, id, link), count, query, since, conversation_id, skipped,
        and notice. The conversation column reads 'chat:<chat_id>' or
        'channel:<team_id>/<channel_id>' — pass those ids to read_teams_messages
        to read the surrounding thread, or the id column to
        get_teams_attachment. The link column is the message's Teams deep link.
        skipped counts hits that could no longer be read.

        You see this as pipe-CSV of those seven columns followed by `count:`,
        `query:`, `since:`, `conversation_id:`, `skipped:` and `notice:` lines
        (empty ones are dropped). No hits renders as `messages: (none)`. The
        notice carries the hashtag-was-stemmed hint, the skipped-messages
        sentence, and the more-results-may-exist hint when they apply.

    Searching is only available on work or school accounts.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    if not query or not query.strip():
        return {"error": "invalid_arguments", "reason": "Provide a search query."}

    try:
        since = teams_ops.normalize_since(since)
    except ValueError as e:
        return {"error": "invalid_date", "reason": str(e)}

    max_results = opt_int(opts.get("max_results"), teams_ops.SEARCH_DEFAULT_MAX_RESULTS)
    max_content_length = opt_int(opts.get("max_content_length"), -1)
    exact_opt = opts.get("exact")
    exact = None if exact_opt is None else opt_bool(exact_opt, True)

    scope_id = conversation_id.strip()
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            found = await teams_ops.asearch_messages(
                client,
                query,
                since=since,
                conversation_id=scope_id,
                max_results=max_results,
                exact=exact,
            )
    except PermissionError as e:
        return _not_connected(e)
    except TeamsSearchUnsupportedError:
        return {
            "error": "search_unsupported",
            "reason": (
                "Teams message search is not available for this account. Microsoft "
                "Search covers work and school accounts only. Read a specific "
                "conversation with read_teams_messages instead."
            ),
        }
    except TeamsNotAvailableError:
        return {
            "error": "teams_not_available",
            "reason": "Microsoft Teams is not available for this account.",
        }

    messages = found["messages"]
    notes: list[str] = []
    # The index stems, so a hashtag query can match messages that never carried
    # the tag literally. Reporting how many were dropped is what makes the
    # exact:false retry an informed choice rather than a guess.
    hydrated = found["candidates"] - found["skipped"]
    if not messages and found["exact"] and found["hashtags"] and hydrated > 0:
        notes.append(
            f"The index matched {hydrated} message(s) but none carried the "
            'hashtag literally; retry with {"exact": false} to see them.'
        )
    if found["skipped"]:
        notes.append(
            f"{found['skipped']} matching message(s) could not be read "
            "(deleted, or no longer shared with you) and were skipped."
        )
    if found["truncated"]:
        notes.append(
            "More results may exist. Narrow the search with since or "
            "conversation_id, or raise max_results."
        )

    rows = [
        {
            "timestamp": msg.get("createdDateTime", ""),
            "sender": extract_message_sender(msg),
            "conversation": msg.get("_conversation", ""),
            "content": extract_message_text(msg, max_length=max_content_length) or "(empty)",
            "attachments": _teams_attachment_column(teams_ops.parse_message_attachments(msg)),
            "id": msg.get("id", ""),
            "link": msg.get("_web_link", ""),
        }
        for msg in messages
    ]

    return {
        "messages": rows,
        "count": len(rows),
        "query": query,
        "since": since,
        "conversation_id": scope_id,
        "skipped": found["skipped"],
        "notice": " ".join(notes),
    }


@mcp.tool(output_schema=None)
async def get_teams_attachment(
    message_id: str,
    attachment_id: str,
    chat_id: str = "",
    team_id: str = "",
    channel_id: str = "",
    mode: str = "text",
    options: str = "",
) -> dict:
    """
    Read, download, or save one attachment from a Teams message.

    The ids come from read_teams_messages' attachments column: a shared file
    shows as `name [file:<id>]`, an inline image as `[image:<id>]`, and a card
    as `[card]`. Pass the id inside the brackets.

    Provide either:
    - chat_id to read from a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to read from a team channel (from list_teams)

    Args:
        message_id: The message ID (the id column of read_teams_messages).
        attachment_id: The file or image ID from that row's attachments column.
        chat_id: Chat ID (from list_chats). Takes priority over team/channel.
        team_id: Team ID (from list_teams with no team_id).
        channel_id: Channel ID (from list_teams with team_id).
        mode: What to return.
            "text" (default) — the extracted text: Word, PowerPoint, Excel, and
                PDF documents are parsed, plain-text files are decoded, cards
                give up their own text, and binaries say why there is no text.
            "metadata" — the attachment record only; nothing is fetched. This
                is the mode that describes a card or a quoted message
                reference, which the other modes have no bytes for.
            "bytes" — the raw bytes, base64-encoded, up to 10 MB.
            "onedrive" — save a copy to your OneDrive and return the link.
            "thumbnail" — a shared file's driveItem thumbnail, base64-encoded.
        options: JSON string with optional fields:
            {"folder_path": "Attachments"}  — OneDrive folder for mode "onedrive"
                (default "Attachments"; created if missing).
            {"site_id": ""}  — save to this SharePoint site's drive instead of OneDrive.
            {"thumbnail": "medium"}  — thumbnail size for mode "thumbnail":
                "small", "medium" (default), or "large".

    Returns:
        "metadata" returns the record read_teams_messages parsed: id, kind
        (file | image | card | message_reference | other), name, content_type,
        content_url, thumbnail_url, and card_text.

        Every other mode returns kind, name, content_type, and size, plus what
        the mode adds: text and truncated for "text" (with reason — "binary" |
        "unsupported" | "too_large" — when there is no text), content_base64
        for "bytes" and "thumbnail", item_id and web_url for "onedrive". A card
        in "text" mode returns kind, content_type, text, and truncated only,
        because there is no file behind it.

        Permanent errors, which must not be retried: invalid_options,
        invalid_mode, invalid_thumbnail, invalid_arguments (neither a chat nor
        a team/channel pair), not_found (the id is not on the message — the
        available list names the ids that are — or it is a kind with no bytes,
        or a file entry with no URL), access_denied (Graph 403 resolving the
        file's sharing link; the owner has to re-share it), is_folder,
        no_thumbnail, too_large (with size and limit, or with reason for the
        download guard), teams_unavailable (the account has no Teams licence),
        and not_connected (with connect_url when one exists). A Graph 404 for
        an unknown chat, channel, or message, throttling, and 5xx propagate as
        tool errors instead — the caller's "transient, retry later" signal.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    mode = mode.strip().lower() or "text"
    # This name's str ancestor called raw bytes "base64"; the synonym keeps
    # callers who memorized that contract working.
    if mode == "base64":
        mode = "bytes"
    if mode not in ("metadata", "text", "bytes", "onedrive", "thumbnail"):
        return {
            "error": "invalid_mode",
            "reason": (
                f"mode must be one of: metadata, text, bytes, onedrive, thumbnail; got {mode!r}"
            ),
        }

    thumb = opt_str(opts.get("thumbnail")) or "medium"
    # Judged before the token so the get_chat_attachment_json alias, which
    # forwards its thumbnail parameter through options, still refuses a bad
    # size with no request — the way its own ancestor did.
    if mode == "thumbnail" and thumb not in _THUMBNAIL_WORDS:
        return {
            "error": "invalid_thumbnail",
            "reason": f"thumbnail must be one of: small, medium, large; got {thumb!r}",
        }

    if not chat_id and not (team_id and channel_id):
        return {
            "error": "invalid_arguments",
            "reason": "Provide either chat_id, or both team_id and channel_id.",
        }
    if chat_id:
        # A chat wins over a team/channel pair, matching read_teams_messages —
        # the ops layer refuses both at once, and that is not the caller's bug.
        team_id = channel_id = ""

    try:
        token = get_graph_token()
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
                return _teams_attachment_not_found(entries)

            kind = entry["kind"]
            if mode == "metadata":
                return dict(entry)
            if kind == "card" and mode == "text":
                return {
                    "kind": "card",
                    "content_type": entry["content_type"],
                    "text": entry["card_text"] or None,
                    "truncated": False,
                }
            if kind not in ("file", "image"):
                return _teams_attachment_not_found(entries)

            if kind == "image":
                # hostedContents advertises no size before $value, so there is
                # nothing to pre-check; the cap below judges what arrived.
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
            else:
                url = entry["content_url"]
                if not url:
                    return {"error": "not_found"}
                try:
                    if mode == "thumbnail":
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
                        name = entry["name"] or item.get("name") or attachment_id
                        content_type = (item.get("file") or {}).get(
                            "mimeType"
                        ) or attachment_ops.guess_content_type(name)
                        raw_size = item.get("size")
                        size = raw_size if isinstance(raw_size, int) else 0

                        # Decided from the driveItem metadata, so an oversized
                        # file is refused before its bytes cross the wire.
                        if mode == "bytes" and size > attachment_ops.MAX_JSON_ATTACHMENT_BYTES:
                            return {
                                "error": "too_large",
                                "size": size,
                                "limit": attachment_ops.MAX_JSON_ATTACHMENT_BYTES,
                            }
                        if mode == "text" and size > files_ops.MAX_DOCUMENT_DOWNLOAD_BYTES:
                            return {
                                "kind": kind,
                                "name": name,
                                "content_type": content_type,
                                "size": size,
                                "text": None,
                                "truncated": False,
                                "reason": "too_large",
                            }
                        try:
                            _, data = await files_ops.aresolve_sharing_link_bytes(
                                client, url, item=item
                            )
                        except ValueError as e:
                            # Only onedrive mode reaches here, having no size
                            # ceiling of its own; the ops download guard still
                            # refuses anything over its own limit.
                            return {"error": "too_large", "reason": str(e)}
                except GraphError as e:
                    if e.status_code == 403:
                        return {"error": "access_denied"}
                    raise

            if mode in ("bytes", "thumbnail"):
                # Images and thumbnails announce no size up front, so the cap is
                # enforced again here on what actually arrived. An inline image
                # ignores the thumbnail size: it is already small, and Graph
                # serves no thumbnail for hosted content.
                if len(data) > attachment_ops.MAX_JSON_ATTACHMENT_BYTES:
                    return {
                        "error": "too_large",
                        "size": len(data),
                        "limit": attachment_ops.MAX_JSON_ATTACHMENT_BYTES,
                    }
                return {
                    "kind": kind,
                    "name": name,
                    "content_type": content_type,
                    "size": len(data),
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }

            att = attachment_ops.ResolvedAttachment(name=name, data=data, content_type=content_type)
            if mode == "onedrive":
                result = await attachment_ops.adeliver_attachment(
                    client,
                    att,
                    "onedrive",
                    folder_path=opt_str(opts.get("folder_path")) or "Attachments",
                    site_id=opt_str(opts.get("site_id")) or "",
                )
                return {
                    "kind": kind,
                    "name": result["name"],
                    "content_type": result["content_type"],
                    "size": result["size"],
                    "item_id": result["item_id"],
                    "web_url": result["web_url"],
                }

            result = await attachment_ops.adeliver_attachment(client, att, "text")
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        # The json ancestor froze this spelling; the Teams read tools say
        # "teams_not_available" instead, and both are permanent.
        return {"error": "teams_unavailable"}

    out = {
        "kind": kind,
        "name": result["name"],
        "content_type": result["content_type"],
        "size": result["size"],
        "text": result["text"],
        "truncated": result["truncated"],
    }
    if "reason" in result:
        out["reason"] = result["reason"]
    return out


@mcp.tool(output_schema=None)
async def send_teams_message(
    message: str,
    team_id: str = "",
    channel_id: str = "",
    chat_id: str = "",
    attachments: str = "",
    options: str = "",
) -> dict:
    """
    Send a message to a Teams channel or chat, with optional @mentions and files.

    Provide either:
    - chat_id to send to a 1:1, group, or meeting chat (from list_chats)
    - team_id + channel_id to send to a team channel (from list_teams)
    chat_id wins when both are given.

    The message is sent as typed: content_type defaults to "text", so a typed
    "<" stays a "<" rather than becoming markup. Pass
    {"content_type": "html"} to send markup, or {"content_type": "auto"} to
    have HTML detected and newlines turned into <br>. @mentions force html.

    Args:
        message: Message content to send. May be empty when files carry it.
        team_id: Team ID (from list_teams with no team_id). Required for channel sending.
        channel_id: Channel ID (from list_teams with team_id). Required for channel sending.
        chat_id: Chat ID (from list_chats). Use this for 1:1 and group chats.
        attachments: JSON array of files carrying their own bytes, e.g.
            [{"name": "notes.txt", "content_base64": "...", "content_type": "text/plain"}].
            content_type is optional and guessed from the name when absent.
            Empty string sends no files.
        options: JSON string with optional fields:
            {"content_type": "text|html|auto",
             "mentions": [{"user_id": "aad-object-id", "name": "Display Name"}],
             "mention_everyone": true,
             "attachments": [{"name": "notes.txt", "text": "..."}],
             "images": [{"name": "chart.png", "base64": "..."}]}
            User IDs (AAD object IDs) can be found in list_teams or list_chats member lists.

            options attachments take the same source specs as send_email — {"name", "text"},
            {"name", "base64"}, {"drive_item_id"}, {"url"} (a sharing link), or
            {"message_id", "attachment_id"}. Teams cannot carry file bytes on a
            message, so each file is uploaded to OneDrive first (a chat: the
            "Microsoft Teams Chat Files" folder, shared read-only with that chat's
            members; a channel: the channel's Files folder) and the message posts a
            card pointing at it. That upload needs the Files.ReadWrite permission.

            images use the same specs but must be image/* under 4 MB; they render
            inline in the message body instead of appearing as files.

    Returns:
        message — the created message, flattened exactly as read_teams_messages
        returns one (id, message_type, from_*, body_content, body_content_type,
        mentioned_user_ids, created, last_modified, attachments) — plus
        sent_to ("chat" or "channel"), and note when mention_everyone was
        ignored because the target was a chat.

        You see this as a confirmation line, the created message's `id:`, a
        `files:` line naming what travelled with it, and the note when present.

        On failure message is null and error says why. Permanent errors:
        invalid_options, invalid_arguments (no ids, or nothing to send),
        invalid_attachments (with a reason naming the offending entry),
        files_scope_missing (the connection cannot write to OneDrive, so no
        file can be sent), teams_unavailable, and not_connected — which is the
        one failure that carries no message key.
    """
    opts, err = parse_options(options)
    if err:
        return {"message": None, "error": "invalid_options", "reason": err}

    if not chat_id and not (team_id and channel_id):
        return {
            "message": None,
            "error": "invalid_arguments",
            "reason": "Provide either chat_id, or both team_id and channel_id.",
        }

    desktop_files, reason = _desktop_attachments(attachments)
    if reason:
        return {"message": None, "error": "invalid_attachments", "reason": reason}

    file_specs = opts.get("attachments")
    image_specs = opts.get("images")
    if not message.strip() and not desktop_files and file_specs is None and image_specs is None:
        return {
            "message": None,
            "error": "invalid_arguments",
            "reason": "message must not be empty",
        }

    # "text", not "auto": a typed "<" must reach the chat as a "<" rather than
    # being read as markup — the same rule update_draft_body holds for mail.
    content_type = opts.get("content_type", "text")
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

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            sent_files: list = []
            sent_images: list = []
            if file_specs is not None:
                try:
                    sent_files = await attachment_ops.aresolve_attachment_sources(
                        client, file_specs
                    )
                except ValueError as e:
                    return {
                        "message": None,
                        "error": "invalid_attachments",
                        "reason": str(e),
                    }
            if image_specs is not None:
                try:
                    sent_images = await attachment_ops.aresolve_attachment_sources(
                        client, image_specs
                    )
                except ValueError as e:
                    # The resolver names the key it was given; this list is "images".
                    return {
                        "message": None,
                        "error": "invalid_attachments",
                        "reason": re.sub(r"^attachments\b", "images", str(e)),
                    }

            if desktop_files or file_specs is not None or image_specs is not None:
                try:
                    created = await teams_ops.asend_message_with_files(
                        client,
                        content=message,
                        content_type=content_type,
                        mentions=mentions_payload,
                        files=[*desktop_files, *sent_files],
                        images=sent_images,
                        chat_id=chat_id,
                        team_id="" if chat_id else team_id,
                        channel_id="" if chat_id else channel_id,
                        exclude_user_id=teams_ops.decode_token_claims(token).get("oid", ""),
                    )
                except ValueError as e:
                    return {
                        "message": None,
                        "error": "invalid_attachments",
                        "reason": str(e),
                    }
            elif chat_id:
                created = await teams_ops.asend_chat_message(
                    client,
                    chat_id,
                    message,
                    content_type=content_type,
                    mentions=mentions_payload,
                )
            else:
                created = await teams_ops.asend_channel_message(
                    client,
                    team_id,
                    channel_id,
                    message,
                    content_type=content_type,
                    mentions=mentions_payload,
                )
    except PermissionError as e:
        # The json ancestor froze the bare not_connected payload here, without
        # the "message" key every other failure carries.
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {
            "message": None,
            "error": "teams_unavailable",
            "reason": "Microsoft Teams is not available for this account.",
        }
    except FilesScopeMissingError:
        return {
            "message": None,
            "error": "files_scope_missing",
            "reason": (
                "Sending files into Teams uploads them to OneDrive first, which needs the "
                "Files.ReadWrite permission this connection lacks (org tenants: ask the "
                "admin to consent to it). Plain messages still work."
            ),
        }

    out = {
        "message": _chat_message_json(created),
        "sent_to": "chat" if chat_id else "channel",
    }
    if everyone_ignored:
        out["note"] = "mention_everyone only works in channels, ignored here."
    return out


def _render_send_teams_message(payload: dict) -> str:
    """Confirm what went out, naming each file so a wrong one is obvious."""
    if "error" in payload:
        # Overrides preempt the error-envelope rule; hand errors back to it.
        return render_compact(payload)

    row = payload["message"] or {}
    lines = [
        f"Message sent to Teams {payload.get('sent_to', 'chat')}.",
        f"id: {row.get('id')}",
    ]
    names = [a["name"] for a in row.get("attachments") or [] if a.get("name")]
    if names:
        lines.append(f"files: {', '.join(names)}")
    if payload.get("note"):
        lines.append(f"note: {payload['note']}")
    return "\n".join(lines)


RENDER_OVERRIDES["send_teams_message"] = _render_send_teams_message


@mcp.tool(output_schema=None)
async def get_teams_activity(hours: int = 24) -> dict:
    """
    Get recent Teams activity across all channels and chats as a digest.

    Scans joined teams' channels and recent chats for messages within the
    specified time window. Ideal for catching up on what you missed.

    Args:
        hours: Look back this many hours (default: 24).

    Returns:
        activity (a row per message: source, source_name, sender, timestamp,
        preview), count, sources (how many distinct chats and channels the rows
        came from), and hours.

        You see this as pipe-CSV of those five columns followed by `count:`,
        `sources:` and `hours:` lines. A quiet window renders as
        `activity: (none)`.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            activity = await teams_ops.aget_teams_activity(client, hours=hours)
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {
            "error": "teams_not_available",
            "reason": "Microsoft Teams is not available for this account.",
        }

    return {
        "activity": activity,
        "count": len(activity),
        "sources": len({row["source_name"] for row in activity}),
        "hours": hours,
    }


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


def _drive_item_row(item: dict) -> dict:
    """One list_files row: the same shape for a folder and a file.

    A folder carries child_count and no mime type; a file carries neither. The
    renderer unions the keys it sees, so the ragged pair reads as one table.
    """
    if "folder" in item:
        return {
            "name": item.get("name", ""),
            "type": "folder",
            "size": item.get("size", 0),
            "child_count": item["folder"].get("childCount", 0),
            "id": item.get("id", ""),
        }
    return {
        "name": item.get("name", ""),
        "type": "file",
        "size": item.get("size", 0),
        "id": item.get("id", ""),
    }


@mcp.tool(output_schema=None)
async def list_sharepoint_sites(query: str = "", top: int = 10) -> dict:
    """
    Search for SharePoint sites, or list followed sites.

    Args:
        query: Search query to find sites (e.g., "engineering"). Leave empty to list followed sites.
        top: Maximum number of results (default: 10).

    Returns:
        sites (a row per site: name, id, web_url), count, and the query as
        passed. Pass a row's id as site_id to list_files, inspect_file, or
        manage_file.

        You see this as pipe-CSV — a `name|id|web_url` header line, one line per
        site — followed by `count:` and `query:` lines (an empty query drops
        out). No matches renders as `sites: (none)`.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            sites = await files_ops.alist_sites(client, query=query, top=top)
    except PermissionError as e:
        return _not_connected(e)

    rows = [
        {
            "name": site.get("displayName", site.get("name", "")),
            "id": site.get("id", ""),
            "web_url": site.get("webUrl", ""),
        }
        for site in sites
    ]
    return {"sites": rows, "count": len(rows), "query": query}


@mcp.tool(output_schema=None)
async def list_files(
    folder_path: str = "", site_id: str = "", query: str = "", url: str = "", top: int = 20
) -> dict:
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

    Returns:
        files (a row per item), count, and whichever of folder_path and query
        was used. Every row carries name, type ("folder" or "file"), size in
        bytes, and id; folders add child_count and search results add summary
        (the matched snippet) and url. Pass a row's id to inspect_file,
        edit_document, or manage_file.

        You see this as pipe-CSV whose header is the union of the row keys
        present — so a browse reads `name|type|size|child_count|id` and a search
        `name|type|size|id|summary|url` — followed by `count:`, `folder_path:`
        and `query:` lines (empty ones are dropped). Nothing found renders as
        `files: (none)`.

        A sharing link that cannot be resolved returns access_denied, not_found,
        or invalid_link.
    """
    sharing_url = url.strip() if url else ""

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            if sharing_url:
                try:
                    items = await files_ops.alist_sharing_link_children(
                        client, sharing_url, top=top
                    )
                except GraphError as e:
                    if e.status_code == 403:
                        return {
                            "error": "access_denied",
                            "reason": (
                                "You don't have permission to access this sharing link. "
                                "The owner may need to re-share it with you."
                            ),
                        }
                    if e.status_code == 404:
                        return {
                            "error": "not_found",
                            "reason": (
                                "No item found for this sharing link. The link may have "
                                "expired, been revoked, or the item was deleted."
                            ),
                        }
                    if e.status_code == 400:
                        return {
                            "error": "invalid_link",
                            "reason": (
                                "Could not resolve this URL. Make sure it's a valid "
                                "SharePoint or OneDrive sharing link."
                            ),
                        }
                    raise
                rows = [_drive_item_row(item) for item in items]
            elif query:
                results = await files_ops.asearch_files_unified(client, query=query, top=top)
                rows = [
                    {
                        **_drive_item_row(item),
                        "summary": item.get("_searchSummary", ""),
                        "url": item.get("webUrl", ""),
                    }
                    for item in results
                ]
            else:
                items = await files_ops.alist_drive_children(
                    client, folder_path=folder_path, site_id=site_id, top=top
                )
                rows = [_drive_item_row(item) for item in items]
    except PermissionError as e:
        return _not_connected(e)

    return {"files": rows, "count": len(rows), "folder_path": folder_path, "query": query}


def _op_summary(op_types: list[str]) -> str:
    """Human-readable summary of applied operations, capped at 5 names."""
    summary = ", ".join(op_types[:5])
    if len(op_types) > 5:
        summary += f" (+{len(op_types) - 5} more)"
    return summary


@mcp.tool(output_schema=None)
async def edit_document(
    item_id: str,
    edits: str,
    site_id: str = "",
    options: str = "",
) -> dict:
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

    Returns:
        Word: ok, kind ("word"), name, operations (how many were applied), ops
        (their op names, first five), track_changes, author, id, web_url.
        Excel: ok, kind ("excel"), name, operations, ops, default_sheet,
        worksheets (comma-separated), web_url.

        Both are all-scalar, so you see one `key: value` line per key.
        A file that is neither .docx nor .xlsx returns invalid_arguments; edits
        that will not parse return invalid_arguments; an edit the document
        rejects (text not found, bad range) returns edit_failed.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            try:
                item = await files_ops.aget_drive_item(client, item_id, site_id=site_id)
            except GraphError as e:
                if e.status_code == 404:
                    return {"error": "not_found", "reason": f"File not found: {item_id}"}
                raise

            name = item.get("name", "")
            lower = name.lower()
            if lower.endswith(".docx"):
                return await _edit_word(client, item, edits, site_id, opts)
            if lower.endswith(".xlsx"):
                return await _edit_excel(client, item, edits, site_id)
            return {
                "error": "invalid_arguments",
                "reason": (
                    f"File '{name}' is not an editable document. Supported types: "
                    ".docx (Word) and .xlsx (Excel). Legacy .xls/.xlsb and macro-enabled "
                    ".xlsm are not supported."
                ),
            }
    except PermissionError as e:
        return _not_connected(e)


async def _edit_word(
    client: AsyncGraphClient,
    item: dict,
    edits: str,
    site_id: str,
    opts: dict,
) -> dict:
    """Edit a .docx via download / apply / re-upload (Track Changes by default)."""
    track_changes = opt_bool(opts.get("track_changes", True), True)
    author = opts.get("author", "Bond AI")

    try:
        operations = document_edit.parse_edits(edits)
    except ValueError as e:
        return {"error": "invalid_arguments", "reason": f"Invalid edits: {e}"}
    if not operations:
        return {"error": "invalid_arguments", "reason": "No edit operations provided."}

    name = item.get("name", "")
    base = files_ops._drive_base(site_id or None)
    doc_bytes = await client.get_bytes(f"{base}/items/{item['id']}/content")

    try:
        modified_bytes = document_edit.apply_edits(
            doc_bytes, operations, track_changes=track_changes, author=author
        )
    except document_edit.EditError as e:
        return {"error": "edit_failed", "reason": str(e)}

    result_item = await files_ops.aupload_bytes_by_id(
        client,
        item_id=item["id"],
        data=modified_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        site_id=site_id,
    )

    return {
        "ok": True,
        "kind": "word",
        "name": name,
        "operations": len(operations),
        "ops": _op_summary([op["op"] for op in operations]),
        "track_changes": track_changes,
        "author": author,
        "id": result_item.get("id", item["id"]),
        "web_url": result_item.get("webUrl", ""),
    }


async def _edit_excel(
    client: AsyncGraphClient,
    item: dict,
    edits: str,
    site_id: str,
) -> dict:
    """Edit an .xlsx in place via the Graph Workbook API (no download/re-upload)."""
    try:
        operations = workbook_edit.parse_workbook_edits(edits)
    except ValueError as e:
        return {"error": "invalid_arguments", "reason": f"Invalid edits: {e}"}
    if not operations:
        return {"error": "invalid_arguments", "reason": "No edit operations provided."}

    name = item.get("name", "")
    base = files_ops._drive_base(site_id or None)

    try:
        result = await workbook_edit.apply_workbook_edits(client, base, item["id"], operations)
    except workbook_edit.EditError as e:
        return {"error": "edit_failed", "reason": str(e)}
    except GraphError as e:
        # The Workbook API reports a rejected op (bad range, protected sheet) as
        # a 4xx, which is the caller's mistake rather than a transient failure.
        return {"error": "edit_failed", "reason": str(e)}

    applied = result["operations"]
    return {
        "ok": True,
        "kind": "excel",
        "name": name,
        "operations": len(applied),
        "ops": _op_summary(applied),
        "default_sheet": result["default_sheet"],
        # Comma-joined rather than a list so the payload stays all-scalar and
        # renders as kv lines instead of falling back to JSON.
        "worksheets": ", ".join(result["worksheets"]),
        "web_url": item.get("webUrl", ""),
    }


@mcp.tool(output_schema=None)
async def manage_file(
    item_id: str = "",
    action: str = "rename",
    new_name: str = "",
    filename: str = "",
    content: str = "",
    content_encoding: str = "",
    options: str = "",
) -> dict:
    """
    Create, copy, rename, or delete a file or folder in OneDrive or SharePoint.

    Actions:
      - "rename" (default): rename a file or folder in place. Requires item_id
        and new_name.
      - "copy": create a server-side copy with a new name — works for any file
        type including Word, Excel, and PDF. Useful for creating a new document
        from a template. Requires item_id and new_name.
      - "delete": move the file or folder to the recycle bin (recoverable from
        the SharePoint/OneDrive UI). Requires item_id; new_name is ignored.
      - "upload": create or overwrite a file. Requires filename and content.
        Uses the simple upload endpoint, so the content must be under 4 MB.

    Upload content modes:
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
        or other binary formats that originate from another source. base64 wins
        over the extension, so a base64 .docx uploads its bytes untouched.

    Note: a workbook edited via edit_document holds a short lock (~1-2 min)
    afterward; rename/copy/delete on it may return "locked" until that clears —
    retry after a moment.

    Args:
        item_id: Drive item ID of the file or folder to act on (from list_files).
            Required for rename, copy, and delete.
        action: "rename" (default), "copy", "delete", or "upload".
        new_name: New name including extension (e.g. "Final-Report.docx").
            Required for rename and copy; ignored otherwise.
        filename: File name including extension for "upload" (e.g. "report.md").
        content: File content for "upload" — plain text, markdown (.docx),
            CSV (.xlsx), or a base64 string.
        content_encoding: Set to "base64" when content is base64-encoded binary
            data. Leave empty for text, markdown, or CSV content.
        options: JSON string with optional fields:
            {"site_id": "..."} — act on this SharePoint site's drive instead of OneDrive.
            {"folder_path": "Documents"} — destination folder for "upload"
                (empty uploads to the drive root).
            {"destination_folder_id": "...", "destination_drive_id": "...", "source_drive_id": "..."}
                — copy targets.

    Returns:
        action, plus per action: rename → id, name, web_url; copy → id (the new
        item), name; delete → id; upload → id, name, size (bytes), web_url.

        Each is all-scalar, so you see one `key: value` line per key. A missing
        item returns not_found; content over the 4 MB simple-upload limit
        returns too_large with the limit in bytes.
    """
    opts, err = parse_options(options)
    if err:
        return {"error": "invalid_options", "reason": err}
    destination_folder_id = opts.get("destination_folder_id", "")
    site_id = opts.get("site_id", "")
    destination_drive_id = opts.get("destination_drive_id", "")
    source_drive_id = opts.get("source_drive_id", "")
    folder_path = opts.get("folder_path", "")

    action = action.lower()
    if action not in ("rename", "copy", "delete", "upload"):
        return {
            "error": "invalid_action",
            "reason": (
                f"Invalid action '{action}'. Must be 'rename', 'copy', 'delete', or 'upload'."
            ),
        }
    if action != "upload" and not item_id:
        return {
            "error": "invalid_arguments",
            "reason": f"item_id is required for the '{action}' action.",
        }
    if action in ("rename", "copy") and not new_name:
        return {
            "error": "invalid_arguments",
            "reason": f"new_name is required for the '{action}' action.",
        }
    if action == "upload" and not filename:
        return {
            "error": "invalid_arguments",
            "reason": "filename is required for the 'upload' action.",
        }

    if action == "upload":
        return await _upload_file(
            filename=filename,
            content=content,
            content_encoding=content_encoding,
            folder_path=folder_path,
            site_id=site_id,
        )

    try:
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
                    return {
                        "action": "copy",
                        "id": status.get("resourceId", ""),
                        "name": new_name,
                    }
                if action == "delete":
                    await files_ops.adelete_drive_item(client, item_id=item_id, site_id=site_id)
                    return {"action": "delete", "id": item_id}

                item = await files_ops.arename_drive_item(
                    client,
                    item_id=item_id,
                    new_name=new_name,
                    site_id=site_id,
                )
                return {
                    "action": "rename",
                    "id": item.get("id", item_id),
                    "name": item.get("name", new_name),
                    "web_url": item.get("webUrl", ""),
                }
            except GraphError as e:
                # Only a 404 is the caller's problem; a throttle or a 5xx must
                # keep propagating so the client retries instead of reading a
                # transient failure as a permanent answer.
                if e.status_code == 404:
                    return {"error": "not_found", "reason": f"File not found: {item_id}"}
                raise
    except PermissionError as e:
        return _not_connected(e)


async def _upload_file(
    filename: str,
    content: str,
    content_encoding: str,
    folder_path: str,
    site_id: str,
) -> dict:
    """The manage_file(action="upload") branch: convert, then simple-upload."""
    is_base64 = content_encoding.lower() == "base64" if content_encoding else False
    lower_name = filename.lower()
    # An explicit base64 encoding beats the extension: the caller already has
    # the finished bytes, so there is nothing to generate from markdown or CSV.
    is_docx = lower_name.endswith(".docx") and not is_base64
    is_xlsx = lower_name.endswith(".xlsx") and not is_base64

    if is_base64:
        try:
            data = base64.b64decode(content)
        except Exception as e:
            return {
                "error": "invalid_arguments",
                "reason": f"Failed to decode base64 content: {e}",
            }
    elif is_docx:
        try:
            data = document_create.markdown_to_docx(content)
        except ValueError as e:
            return {
                "error": "invalid_arguments",
                "reason": f"Failed to generate Word document: {e}",
            }
    elif is_xlsx:
        try:
            data = document_create.csv_to_xlsx(content)
        except ValueError as e:
            return {
                "error": "invalid_arguments",
                "reason": f"Failed to generate Excel workbook: {e}",
            }

    try:
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
    except PermissionError as e:
        return _not_connected(e)
    except ValueError as e:
        # The ops layer refuses anything over the simple-upload cap before it
        # sends a byte, so this never races a half-written file.
        return {
            "error": "too_large",
            "limit": files_ops.MAX_SIMPLE_UPLOAD_BYTES,
            "reason": str(e),
        }

    return {
        "action": "upload",
        "id": item.get("id", ""),
        "name": filename,
        "size": item.get("size", 0),
        "web_url": item.get("webUrl", ""),
    }


# ---------------------------------------------------------------------------
# Power BI
# ---------------------------------------------------------------------------


@mcp.tool(output_schema=None)
async def list_powerbi(workspace_id: str = "", content_type: str = "all") -> dict:
    """
    List Power BI workspaces, or the datasets, reports, and dashboards in one.

    With no workspace_id, lists every workspace the user can reach. With a
    workspace_id (or "me" for My workspace), lists that workspace's content.

    Args:
        workspace_id: The workspace ID to list content for. Use "me" for My
            workspace. Leave empty to list the workspaces themselves.
        content_type: What to list within a workspace: "datasets", "reports",
            "dashboards", or "all" (default). Ignored when listing workspaces.

    Returns:
        Workspace mode: workspaces (a row per workspace: name, id, premium) and
        count. Content mode: items (a row per item: kind — "dataset", "report",
        or "dashboard" — name, id, plus refreshable on datasets and dataset_id
        on reports), count, and workspace_id.

        You see this as pipe-CSV whose header is the union of the row keys
        present, followed by `count:` and, in content mode, `workspace_id:`.
        An empty workspace renders as `items: (none)`. Pass a dataset id to
        query_dataset or refresh_dataset, and a report id to export_report.
    """
    if not workspace_id:
        try:
            token = get_powerbi_token()
            async with AsyncPowerBIClient(token) as client:
                workspaces = await pbi_ops.alist_workspaces(client)
        except PermissionError as e:
            return _not_connected(e)

        # My workspace exists for every user but has no group ID, so Graph never
        # lists it — prepend it under the sentinel id the other tools accept.
        rows = [{"name": "My workspace", "id": "me", "premium": False}]
        rows.extend(
            {
                "name": ws.get("name", ""),
                "id": ws.get("id", ""),
                "premium": bool(ws.get("isOnDedicatedCapacity")),
            }
            for ws in workspaces
        )
        return {"workspaces": rows, "count": len(rows)}

    content_type = content_type.lower()
    if content_type not in ("datasets", "reports", "dashboards", "all"):
        return {
            "error": "invalid_arguments",
            "reason": (
                f"Invalid content_type '{content_type}'. "
                "Must be: datasets, reports, dashboards, or all."
            ),
        }

    ws = "" if workspace_id.lower() == "me" else workspace_id
    try:
        token = get_powerbi_token()
        async with AsyncPowerBIClient(token) as client:
            datasets = (
                await pbi_ops.alist_datasets(client, ws)
                if content_type in ("datasets", "all")
                else []
            )
            reports = (
                await pbi_ops.alist_reports(client, ws)
                if content_type in ("reports", "all")
                else []
            )
            dashboards = (
                await pbi_ops.alist_dashboards(client, ws)
                if content_type in ("dashboards", "all")
                else []
            )
    except PermissionError as e:
        return _not_connected(e)

    rows: list[dict] = [
        {
            "kind": "dataset",
            "name": ds.get("name", ""),
            "id": ds.get("id", ""),
            "refreshable": bool(ds.get("isRefreshable")),
        }
        for ds in datasets
    ]
    rows.extend(
        {
            "kind": "report",
            "name": r.get("name", ""),
            "id": r.get("id", ""),
            "dataset_id": r.get("datasetId", ""),
        }
        for r in reports
    )
    rows.extend(
        {"kind": "dashboard", "name": d.get("displayName", ""), "id": d.get("id", "")}
        for d in dashboards
    )

    return {"items": rows, "count": len(rows), "workspace_id": workspace_id}


@mcp.tool(output_schema=None)
async def query_dataset(workspace_id: str, dataset_id: str, dax_query: str) -> dict:
    """
    Execute a DAX query against a Power BI dataset.

    The dataset must be on Premium or Fabric capacity and you must have Build
    permission on the dataset.

    Args:
        workspace_id: The workspace ID (from list_powerbi). Use "me" for My workspace.
        dataset_id: The dataset ID (from list_powerbi with a workspace_id).
        dax_query: A valid DAX query (e.g., "EVALUATE TOPN(10, 'Sales', 'Sales'[Amount], DESC)").

    Returns:
        rows (the query result rows exactly as Power BI returned them, keyed by
        DAX column name such as 'Sales'[Region]) and count.

        You see this as pipe-CSV. Power BI omits null-valued columns from a row
        rather than sending an empty cell, so rows can carry different key sets;
        the header is the union of them in first-seen order and an omitted
        column renders as an empty cell. No rows renders as `rows: (none)`.
    """
    ws = "" if workspace_id.lower() == "me" else workspace_id
    try:
        token = get_powerbi_token()
        async with AsyncPowerBIClient(token) as client:
            result = await pbi_ops.aexecute_dax_query(client, ws, dataset_id, dax_query)
    except PermissionError as e:
        return _not_connected(e)

    try:
        rows = result["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError):
        rows = []
    return {"rows": rows, "count": len(rows)}


@mcp.tool(output_schema=None)
async def refresh_dataset(workspace_id: str, dataset_id: str) -> dict:
    """
    Trigger an on-demand refresh of a Power BI dataset.

    Starts the refresh and returns immediately — the refresh runs in the
    background. Use list_powerbi to find refreshable datasets.

    Args:
        workspace_id: The workspace ID (from list_powerbi). Use "me" for My workspace.
        dataset_id: The dataset ID (from list_powerbi with a workspace_id).

    Returns:
        ok, dataset_id, and workspace_id — three `key: value` lines.

        There is no refresh id to return: the Power BI trigger endpoint answers
        202 with an empty body. Refresh progress lives in the dataset's refresh
        history, which is a separate call this tool deliberately does not make.
    """
    ws = "" if workspace_id.lower() == "me" else workspace_id
    try:
        token = get_powerbi_token()
        async with AsyncPowerBIClient(token) as client:
            await pbi_ops.atrigger_refresh(client, ws, dataset_id)
    except PermissionError as e:
        return _not_connected(e)

    return {"ok": True, "dataset_id": dataset_id, "workspace_id": workspace_id}


@mcp.tool(output_schema=None)
async def export_report(
    workspace_id: str,
    report_id: str,
    export_format: str = "PDF",
    pages: str = "",
    folder_path: str = "Power BI Exports",
) -> dict:
    """
    Export a Power BI report to PDF, PNG, or PPTX and save it to OneDrive.

    Exports the report, downloads the file, and uploads it to the user's OneDrive
    so it can be shared or attached to other workflows. Requires the workspace to
    be on Premium or Fabric capacity.

    Args:
        workspace_id: The workspace ID (from list_powerbi). Use "me" for My workspace.
        report_id: The report ID (from list_powerbi with a workspace_id).
        export_format: "PDF" (default), "PNG", or "PPTX".
        pages: Comma-separated page names to export (e.g., "ReportSection1,ReportSection2").
               Leave empty to export all pages.
        folder_path: OneDrive folder to save the export to (default: "Power BI Exports").

    Returns:
        ok, format, filename, size (bytes), folder_path, item_id (the OneDrive
        drive item), and web_url — one `key: value` line per key. Pass item_id
        to manage_file or inspect_file.

        An unknown format returns invalid_arguments. If the export succeeds but
        the Microsoft connection cannot save it, the answer is a not_connected
        error whose reason says the bytes were exported and lost.
    """
    _mime_types = {
        "PDF": "application/pdf",
        "PNG": "image/png",
        "PPTX": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }

    export_format = export_format.upper()
    if export_format not in _mime_types:
        return {
            "error": "invalid_arguments",
            "reason": f"Invalid export_format '{export_format}'. Must be: PDF, PNG, or PPTX.",
        }

    page_list = [p.strip() for p in pages.split(",") if p.strip()] if pages else None
    ws = "" if workspace_id.lower() == "me" else workspace_id

    # Step 1: Export from Power BI (uses PBI token)
    try:
        pbi_token = get_powerbi_token()
        async with AsyncPowerBIClient(pbi_token) as pbi_client:
            export_id = await pbi_ops.astart_export(
                pbi_client, ws, report_id, export_format, pages=page_list
            )
            status = await pbi_ops.apoll_export(pbi_client, ws, report_id, export_id)
            file_bytes = await pbi_ops.adownload_export(pbi_client, ws, report_id, export_id)
    except PermissionError as e:
        return _not_connected(e)

    ext = status.get("resourceFileExtension", f".{export_format.lower()}")
    filename = f"report-{report_id}{ext}"
    content_type = _mime_types[export_format]

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
    except PermissionError as e:
        return {
            **_not_connected(e),
            "reason": (
                f"Report exported as {export_format} ({len(file_bytes)} bytes) but could "
                "not be saved to OneDrive because the Microsoft connection is not active."
            ),
        }

    return {
        "ok": True,
        "format": export_format,
        "filename": filename,
        "size": len(file_bytes),
        "folder_path": folder_path,
        "item_id": item.get("id", ""),
        "web_url": item.get("webUrl", ""),
    }


# ---------------------------------------------------------------------------
# Dict-returning tools
#
# Unlike read_email, the one str-returning tool above, these return a canonical dict.
# FormatNegotiation hands that dict to a programmatic caller (the desktop mail
# app) as structuredContent and renders it compactly for everyone else.
# Parameters remain str/int only.
#
# Error contract: a missing Microsoft connection returns the not_connected
# payload (with a connect URL when one exists). The Teams write tools return
# a structured "teams_unavailable" error for the no-license 403, which is
# permanent and must not be retried. The mail attachment tools (
# get_mail_attachment, add_draft_attachment_json) likewise return structured
# permanent errors — invalid_mode, invalid_options, too_large, reference,
# empty_name, invalid_base64 — which must not be retried either. The Teams
# attachment reader (get_teams_attachment) returns not_found (with the
# available ids), access_denied, no_thumbnail, invalid_thumbnail, is_folder,
# too_large, invalid_mode, invalid_options, invalid_arguments, and
# teams_unavailable; inspect_file returns missing_target, access_denied,
# not_found, and invalid_link — all permanent.
# send_teams_message returns invalid_attachments and files_scope_missing
# (the account's connection lacks Files.ReadWrite) — both permanent. The mail
# tools return external_sender when the mail sender policy hides a message —
# also permanent, and never accompanied by any detail of the hidden message;
# connection_status reports mail_policy.enabled so a client can explain the
# gap and resync when it flips. The three paging tools (sync_mail, list_chats,
# read_teams_messages) return invalid_cursor when the
# cursor is not a Graph URL — permanent; the Graph client refuses to send the
# bearer token anywhere else. send_draft reads the draft's ids before sending
# and returns them, so a client can store its own copy of the sent mail.
# search_people returns directory_scope_missing when the connection lacks
# User.ReadBasic.All; ensure_chat returns invalid_members (an id that is
# not a Graph user id or UPN), no_identity (the caller cannot be read off the
# token), and no_members (nobody left after dropping blanks and the caller),
# plus teams_unavailable — all permanent. list_chats and read_teams_messages
# return the same no_identity when a mark-as-read option cannot name the user,
# invalid_date for a malformed since, and teams_unavailable (with a reason) for
# the no-licence 403. list_teams, get_teams_activity, and search_teams_messages
# spell that same 403 teams_not_available, and search_teams_messages adds
# search_unsupported for the consumer accounts
# Microsoft Search does not index — both permanent. list_files maps an
# unusable sharing link to access_denied, not_found, or invalid_link;
# manage_file returns not_found for a missing item and too_large (with the
# byte limit) for content over the simple-upload cap; edit_document returns
# edit_failed when the document rejects an operation — all permanent, and all
# reached from narrow exception clauses so every other Graph failure still
# propagates.
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


@mcp.tool(output_schema=None)
async def get_profile() -> dict:
    """
    Get the signed-in user's identity.

    Returns id, display_name, mail, user_principal_name, mailbox_address, and
    job_title. Every key is always present; mailbox_address and job_title are
    null when Graph does not supply them.

    IMPORTANT: when mailbox_address is set, use it as the from_address when
    sending email. That is the address the mail server is authorized to send
    from, and using it avoids "via" warnings and spam filtering.

    The id is the Graph user object ID — Teams messages carry the same ID, so
    clients use it to tell their own messages apart from everyone else's.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            profile = await mail_ops.aget_profile(client)
    except PermissionError as e:
        return _not_connected(e)
    return {
        **_profile_json(profile),
        "mailbox_address": profile.get("mailboxAddress"),
        "job_title": profile.get("jobTitle"),
    }


@mcp.tool(output_schema=None)
async def search_people(query: str, top: int = 10) -> dict:
    """
    Search the organisation directory by name or mail prefix.

    Matches people whose display name has a word starting with the query or
    whose mail starts with it, ordered by display name. Returns people: a list
    of {id, display_name, mail, user_principal_name, job_title}, rendered as
    pipe-CSV with those columns; mail and job_title may be null. The signed-in
    user can appear in the results — a caller building a recipient typeahead
    filters them out if it wants to. A blank query, or one with nothing
    searchable left once "&" is dropped, returns an empty list without calling
    Graph.

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


@mcp.tool(output_schema=None)
async def sync_mail(folder: str = "inbox", cursor: str = "", min_received: str = "") -> dict:
    """
    Fetch ONE page of a mail folder's delta feed, for incremental sync.

    Returns messages (raw Graph message objects, including `@removed`
    tombstones for deletions), next_cursor (more pages in this run),
    delta_cursor (this run is done — save it and pass it back next time), and
    resync. The rows keep Graph's own nested camelCase shape, so they render as
    compact JSON rather than CSV; use list_emails for a readable listing.

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
        logger.warning("sync_mail: refused a non-Graph cursor")
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


@mcp.tool(output_schema=None)
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
    get_mail_attachment for content. At most 50 attachments are listed,
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
    mailbox: str | None = None,
) -> dict:
    """text mode for get_mail_attachment: extract, decode, or explain.

    ``expanded`` is the already-fetched item attachment when the mail policy
    had to fetch it to judge the attached message, so it is never fetched twice.
    """
    if summary["kind"] == "reference":
        return {**summary, "text": None, "truncated": False, "reason": "reference"}
    if summary["kind"] == "item":
        if expanded is None:
            expanded = await attachment_ops.aget_item_attachment(
                client, message_id, attachment_id, mailbox=mailbox
            )
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
        client, message_id, attachment_id, mailbox=mailbox
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


@mcp.tool(output_schema=None)
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


@mcp.tool(output_schema=None)
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


@mcp.tool(output_schema=None)
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


@mcp.tool(output_schema=None)
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


@mcp.tool(output_schema=None)
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


@mcp.tool(output_schema=None)
async def mark_mail_read(message_ids: str, is_read: str = "true") -> dict:
    """
    Mark messages read (or unread) in bulk.

    Best effort per message: a message that no longer exists is reported in
    failed rather than failing the whole call. Returns updated (how many were
    patched) and failed (one entry per message that was not), the latter
    rendered as pipe-CSV with columns id|error. At most 100 IDs are processed
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


@mcp.tool(output_schema=None)
async def get_chat_members(chat_id: str) -> dict:
    """
    List a chat's members.

    Returns members: the chat's full member list, each entry with user_id and
    display_name, rendered as pipe-CSV with those columns. Use it to label 1:1
    chats, which have no topic.

    Args:
        chat_id: The chat ID (from list_chats).
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


@mcp.tool(output_schema=None)
async def ensure_chat(user_ids: str, topic: str = "") -> dict:
    """
    Find or create a Teams chat with the given people.

    Use it to message someone who has no chat yet. Pass one id and Graph
    returns the existing 1:1 chat with that person if there is one, otherwise
    creates it — calling this twice is safe. Pass two or more ids and Graph
    creates a NEW group chat every call (group chats are never de-duplicated);
    topic applies only to a group chat. The signed-in user is always a member
    and need not be listed. Requires Chat.ReadWrite.

    Returns chat_id and chat_type ("oneOnOne" or "group"). Permanent errors:
    invalid_members (an id is not a Graph user id or UPN), no_identity (the
    signed-in user cannot be read off the token), no_members (nobody left
    after dropping blanks and the caller), teams_unavailable (no Teams
    license). A Graph 400 on an unknown user propagates as a tool error.

    Args:
        user_ids: Comma-separated Graph user ids or user principal names.
            Prefer the id search_people returns: a UPN with a character
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


@mcp.tool(output_schema=None)
async def mark_chat_read(chat_id: str) -> dict:
    """
    Mark a Teams chat read for the signed-in user.

    Read state in Teams is per CHAT, not per message: it is a viewpoint on the
    conversation, so this marks the chat read up to its newest message and
    there is no way to ack one message and leave a later one unread. Requires
    Chat.ReadWrite.

    Returns ok: true on success. On failure ok is false and error says why —
    "no_identity" when the signed-in user cannot be read off the token,
    "teams_unavailable" when the account has no Teams license.

    Args:
        chat_id: The chat ID (from list_chats).
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


@mcp.tool(output_schema=None)
async def inspect_file(
    item_id: str = "", url: str = "", read_content: str = "false", site_id: str = ""
) -> dict:
    """
    Get a file's metadata from OneDrive or SharePoint, and optionally its text.

    Accepts either a drive item id (from list_files) or a SharePoint/OneDrive
    sharing URL pasted from the browser or Teams. An item_id that looks like a
    sharing URL is treated as one. This is also how a Teams attachment's
    content_url or a mail link attachment's source_url is resolved into
    something readable.

    Returns item_id, name, size, content_type, web_url, modified, and
    is_folder. With read_content "true" it also returns text — extracted from
    Word, PowerPoint, Excel, and PDF documents (up to 50 MB; tables and notes
    included, images noted but not shown), or decoded for text files (up to
    2 MB) — and null when the content is binary, a folder, or over those
    limits.

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


@mcp.tool(output_schema=None)
async def connection_status() -> dict:
    """
    Report whether Microsoft is connected, and with which scopes.

    For programmatic clients deciding what to show before any real call.
    Returns connected, scopes (bare lowercased names, e.g. "mail.read"),
    connect_url (set only when disconnected), and account ({id, display_name,
    mail, user_principal_name}, or null if the profile could not be fetched —
    get_profile returns the same four keys plus mailbox_address and job_title).

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


# ---------------------------------------------------------------------------
# Deprecated aliases
#
# Every renamed tool answers to its old name here. HideDeprecatedAliases keeps
# these out of tools/list, so a model never sees two names for one tool, but a
# call still works — bond-desktop's call sites move over on its own release
# schedule, and these come out once it has. Each forwarder keeps its ancestor's
# exact signature and declares output_schema=None, without which the alias
# advertises a generated schema and FormatNegotiation has to pass it through
# uncompacted.
# ---------------------------------------------------------------------------


@mcp.tool(name="get_user_profile", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_get_user_profile() -> dict:
    """Deprecated alias for get_profile."""
    return await get_profile()


@mcp.tool(name="get_profile_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_get_profile_json() -> dict:
    """Deprecated alias for get_profile."""
    return await get_profile()


@mcp.tool(name="search_people_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_search_people_json(query: str, top: int = 10) -> dict:
    """Deprecated alias for search_people."""
    return await search_people(query, top=top)


@mcp.tool(name="list_mail_delta", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_list_mail_delta(
    folder: str = "inbox", cursor: str = "", min_received: str = ""
) -> dict:
    """Deprecated alias for sync_mail."""
    return await sync_mail(folder=folder, cursor=cursor, min_received=min_received)


@mcp.tool(name="mark_mail_read_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_mark_mail_read_json(message_ids: str, is_read: str = "true") -> dict:
    """Deprecated alias for mark_mail_read."""
    return await mark_mail_read(message_ids, is_read=is_read)


@mcp.tool(name="get_chat_members_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_get_chat_members_json(chat_id: str) -> dict:
    """Deprecated alias for get_chat_members."""
    return await get_chat_members(chat_id)


@mcp.tool(name="ensure_chat_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_ensure_chat_json(user_ids: str, topic: str = "") -> dict:
    """Deprecated alias for ensure_chat."""
    return await ensure_chat(user_ids, topic=topic)


@mcp.tool(name="mark_chat_read_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_mark_chat_read_json(chat_id: str) -> dict:
    """Deprecated alias for mark_chat_read."""
    return await mark_chat_read(chat_id)


@mcp.tool(name="inspect_file_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_inspect_file_json(
    item_id: str = "", url: str = "", read_content: str = "false", site_id: str = ""
) -> dict:
    """Deprecated alias for inspect_file."""
    return await inspect_file(item_id=item_id, url=url, read_content=read_content, site_id=site_id)


@mcp.tool(name="upload_file", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_upload_file(
    filename: str,
    content: str,
    folder_path: str = "",
    site_id: str = "",
    content_encoding: str = "",
) -> dict:
    """Deprecated alias for manage_file(action="upload")."""
    opts = {}
    if folder_path:
        opts["folder_path"] = folder_path
    if site_id:
        opts["site_id"] = site_id
    return await manage_file(
        action="upload",
        filename=filename,
        content=content,
        content_encoding=content_encoding,
        options=json.dumps(opts) if opts else "",
    )


@mcp.tool(name="list_powerbi_workspaces", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_list_powerbi_workspaces() -> dict:
    """Deprecated alias for list_powerbi."""
    return await list_powerbi()


@mcp.tool(name="list_powerbi_content", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_list_powerbi_content(workspace_id: str, content_type: str = "all") -> dict:
    """Deprecated alias for list_powerbi."""
    return await list_powerbi(workspace_id=workspace_id, content_type=content_type)


@mcp.tool(name="get_email_attachment", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_get_email_attachment(
    message_id: str, attachment_id: str, mode: str = "text", mailbox: str = "", options: str = ""
) -> dict:
    """Deprecated alias for get_mail_attachment."""
    # "base64" was this name's word for raw bytes; the synonym lives only here.
    if mode.strip().lower() == "base64":
        mode = "bytes"
    return await get_mail_attachment(message_id, attachment_id, mode, mailbox, options)


@mcp.tool(name="get_mail_attachment_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_get_mail_attachment_json(
    message_id: str, attachment_id: str, mode: str = "bytes"
) -> dict:
    """Deprecated alias for get_mail_attachment."""
    return await get_mail_attachment(message_id, attachment_id, mode)


@mcp.tool(name="get_chat_attachment_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_get_chat_attachment_json(
    chat_id: str, message_id: str, attachment_id: str, thumbnail: str = ""
) -> dict:
    """Deprecated alias for get_teams_attachment."""
    # thumbnail was a flat parameter; it now rides the options JSON, and an
    # empty one means the full bytes rather than a size word.
    thumb = thumbnail.strip().lower()
    mode = "thumbnail" if thumb else "bytes"
    options = json.dumps({"thumbnail": thumb}) if thumb else ""
    return await get_teams_attachment(
        message_id, attachment_id, chat_id=chat_id, mode=mode, options=options
    )


@mcp.tool(name="list_chats_page", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_list_chats_page(cursor: str = "", top: int = 50) -> dict:
    """Deprecated alias for list_chats."""
    return await list_chats(cursor=cursor, top=top)


@mcp.tool(name="list_chat_messages_page", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_list_chat_messages_page(chat_id: str, since: str = "", cursor: str = "") -> dict:
    """Deprecated alias for read_teams_messages."""
    # The page option pins the json ancestor's semantics: one page, newest
    # first, `since` filtering last-modified rather than creation time.
    return await read_teams_messages(
        chat_id=chat_id, since=since, cursor=cursor, options='{"page": true}'
    )


@mcp.tool(name="send_chat_message_json", tags={DEPRECATED_ALIAS_TAG}, output_schema=None)
async def _alias_send_chat_message_json(chat_id: str, text: str, attachments: str = "") -> dict:
    """Deprecated alias for send_teams_message."""
    # These two error strings are part of the frozen json contract; the merged
    # tool speaks invalid_arguments instead, so they live only here.
    if not chat_id.strip():
        return {"message": None, "error": "chat_id must not be empty"}
    if not text.strip() and not attachments.strip():
        return {"message": None, "error": "text must not be empty"}
    return await send_teams_message(message=text, chat_id=chat_id, attachments=attachments)


if __name__ == "__main__":
    mcp.run()
