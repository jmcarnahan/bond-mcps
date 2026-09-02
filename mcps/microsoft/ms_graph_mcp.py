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

Tool summary (39 tools):
  Email     : get_user_profile, list_emails, read_email, send_email, manage_inbox_rules, manage_mail_folders
  Calendar  : list_calendar_events, get_calendar_event, create_calendar_event, check_availability
  Teams     : list_teams, list_chats, read_teams_messages, send_teams_message, get_teams_activity
  Files     : list_sharepoint_sites, list_files, inspect_file, upload_file, edit_document, manage_file
  Power BI  : list_powerbi_workspaces, list_powerbi_content, query_dataset, refresh_dataset, export_report
  Desktop JSON : get_profile_json, list_mail_delta, get_mail_detail, create_reply_draft_json,
                 update_draft_body, send_draft, mark_mail_read_json, list_chats_page,
                 get_chat_members_json, list_chat_messages_page, mark_chat_read_json,
                 send_chat_message_json, connection_status

The 26 markdown tools above render prose for an LLM to read. The Desktop JSON
namespace is for programmatic clients (the desktop mail app) and follows a
different convention: every tool returns a ``dict``, which FastMCP surfaces as
structuredContent. Parameters stay ``str``/``int`` only (empty string = absent)
for Bedrock compatibility, as everywhere else in this server.
"""

import base64
import html as html_mod
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import JSONResponse

load_dotenv(Path(__file__).parent / ".env")

from ms_graph import calendar as calendar_ops
from ms_graph import document_create, document_edit, workbook_edit
from ms_graph import files as files_ops
from ms_graph import folders as folder_ops
from ms_graph import mail as mail_ops
from ms_graph import power_bi as pbi_ops
from ms_graph import teams as teams_ops
from ms_graph.auth import get_graph_token, get_powerbi_token
from ms_graph.graph_client import AsyncGraphClient, GraphError
from ms_graph.local_auth import login_scopes
from ms_graph.power_bi import AsyncPowerBIClient
from ms_graph.teams import TeamsNotAvailableError, extract_message_sender, extract_message_text

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
    """
    import csv
    import io

    opts, err = parse_options(options)
    if err:
        return err

    _SELECT = "id,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview"

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

        mark_ids = opts.get("mark_as_read", [])
        if mark_ids:
            if not isinstance(mark_ids, list):
                return "Option 'mark_as_read' must be a JSON array of message IDs."
            for mid in mark_ids:
                await mail_ops.amark_read(client, mid, mailbox=mb)

    if not messages:
        prefix = f'No messages found matching "{query}".' if query else "No messages found."
        if mark_ids and isinstance(mark_ids, list):
            return f"{prefix}\n\n{len(mark_ids)} message(s) marked as read."
        return prefix

    output = io.StringIO()
    if query:
        output.write(f'{len(messages)} result(s) for "{query}"\n\n')
    else:
        output.write(f"{len(messages)} message(s) in {folder}\n\n")

    writer = csv.writer(output, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(
        ["date", "from_name", "from_address", "to", "subject", "is_read", "body_preview", "id"]
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
                msg.get("id", ""),
            ]
        )

    if mark_ids:
        output.write(f"\n{len(mark_ids)} message(s) marked as read.")

    return output.getvalue()


@mcp.tool()
async def read_email(message_id: str, mailbox: str = "", options: str = "") -> str:
    """
    Read a single email message by its ID.

    Args:
        message_id: The Graph API message ID (from list_emails output).
        mailbox: Shared mailbox email address (e.g. "support@company.com"). Leave empty
            to access your own mailbox. Requires Mail.Read.Shared permission and Exchange
            Full Access delegation on the shared mailbox.
        options: JSON string with optional fields:
            {"mark_as_read": true/false}  — mark the email read (or unread) after reading.
            {"max_content_length": -1}  — max characters for the email body. Default -1
                (no limit). Set a positive integer to truncate long emails.
    """
    opts, err = parse_options(options)
    if err:
        return err

    max_content_length = opt_int(opts.get("max_content_length"), -1)

    mb = mailbox or None
    token = get_graph_token()
    async with AsyncGraphClient(token) as client:
        msg = await mail_ops.aget_message(client, message_id, mailbox=mb)
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

    if mark is not None:
        state = "read" if opt_bool(mark, True) else "unread"
        result += f"\n\n---\n*Marked as {state}.*"

    return result


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

    async with AsyncGraphClient(token) as client:
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
        )

    cc_note = f" (CC: {cc})" if cc else ""
    bcc_note = f" (BCC: {len(bcc_list)} recipients)" if bcc_list else ""
    source = f" from {mailbox}" if mb else ""
    return f"Email sent to {to}{source}{cc_note}{bcc_note}."


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
        create/update: confirmation message with rule ID and name.
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
    writer.writerow(["timestamp", "sender", "content", "id"])
    for msg in messages:
        sender = extract_message_sender(msg)
        content = extract_message_text(msg, max_length=max_content_length)
        writer.writerow(
            [
                msg.get("createdDateTime", ""),
                sender,
                content or "(empty)",
                msg.get("id", ""),
            ]
        )

    result = f"{len(messages)} message(s) in {source} since {since}\n{buf.getvalue()}"
    if should_mark:
        result += "\n---\n*Chat marked as read.*"
    return result


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
             "mention_everyone": true}
            User IDs (AAD object IDs) can be found in list_teams or list_chats member lists.
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

    token = get_graph_token()
    try:
        async with AsyncGraphClient(token) as client:
            if chat_id:
                await teams_ops.asend_chat_message(
                    client,
                    chat_id,
                    message,
                    content_type=content_type,
                    mentions=mentions_payload,
                )
                note = (
                    " (Note: mention_everyone only works in channels, ignored here.)"
                    if everyone_ignored
                    else ""
                )
                return f"Message sent to Teams chat.{note}"
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
# the 23 markdown tools above, these return dicts — FastMCP renders them as
# structuredContent. Parameters remain str/int only.
#
# Error contract: a missing Microsoft connection returns the not_connected
# payload (with a connect URL when one exists). The Teams write tools return
# a structured "teams_unavailable" error for the no-license 403, which is
# permanent and must not be retried. Everything else — Graph 5xx, throttling,
# unexpected shapes — propagates so FastMCP raises a tool error, which is the
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


def _chat_message_json(msg: dict) -> dict:
    """Flatten a Graph chatMessage. Every nested object can be null on system
    events, so each level is read defensively."""
    sender = msg.get("from") or {}
    user = sender.get("user") or {}
    application = sender.get("application") or {}
    body = msg.get("body") or {}
    return {
        "id": msg.get("id"),
        "message_type": msg.get("messageType"),
        "from_user_id": user.get("id"),
        "from_user_display": user.get("displayName"),
        "from_application_id": application.get("id"),
        "body_content": body.get("content"),
        "body_content_type": body.get("contentType"),
        "created": msg.get("createdDateTime"),
        "last_modified": msg.get("lastModifiedDateTime"),
    }


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
    for the folder and call again with an empty cursor.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            data = await mail_ops.adelta_page(
                client, folder=folder, cursor=cursor, min_received=min_received
            )
    except PermissionError as e:
        return _not_connected(e)
    except GraphError as e:
        if e.status_code == 410:
            return {"messages": [], "next_cursor": "", "delta_cursor": "", "resync": True}
        raise

    return {
        "messages": data.get("value", []),
        "next_cursor": data.get("@odata.nextLink", ""),
        "delta_cursor": data.get("@odata.deltaLink", ""),
        "resync": False,
    }


@mcp.tool()
async def get_mail_detail(message_id: str) -> dict:
    """
    Get a message's plain-text body and internet headers as structured JSON.

    For programmatic clients. Exchange converts the body to text server-side,
    so the client never parses HTML. Returns body_text (the reply-relevant
    part only, without the quoted thread), headers (lowercased name → value;
    where a header repeats, the first occurrence wins), and has_attachments.

    Args:
        message_id: The Graph message ID.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            msg = await mail_ops.aget_message_detail(client, message_id)
    except PermissionError as e:
        return _not_connected(e)

    headers: dict[str, str] = {}
    for header in msg.get("internetMessageHeaders") or []:
        name = (header.get("name") or "").lower()
        if name and name not in headers:
            headers[name] = header.get("value")

    return {
        "body_text": (msg.get("uniqueBody") or {}).get("content") or "",
        "headers": headers,
        "has_attachments": bool(msg.get("hasAttachments")),
    }


@mcp.tool()
async def create_reply_draft_json(message_id: str, timezone: str = "") -> dict:
    """
    Create a reply draft for a message and return its ID as structured JSON.

    For programmatic clients. Graph builds the draft with recipients and the
    quoted original already filled in; use update_draft_body to set the reply
    text, then send_draft. Returns id and web_link.

    Args:
        message_id: The Graph message ID to reply to.
        timezone: IANA or Windows timezone for the quoted original's
            timestamps (e.g. "America/New_York"). Empty leaves them in UTC.
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            draft = await mail_ops.acreate_reply_draft(client, message_id, timezone=timezone)
    except PermissionError as e:
        return _not_connected(e)
    return {"id": draft.get("id", ""), "web_link": draft.get("webLink", "")}


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
async def send_draft(draft_id: str) -> dict:
    """
    Send an existing draft. Returns structured JSON.

    For programmatic clients. Graph accepts the send asynchronously, so a
    successful return means "queued", not "delivered".

    Args:
        draft_id: The draft message ID (from create_reply_draft_json).
    """
    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            await mail_ops.asend_draft(client, draft_id)
    except PermissionError as e:
        return _not_connected(e)
    return {"ok": True}


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

    For programmatic clients. Returns one page of up to 50 members, each with
    user_id and display_name. Use it to label 1:1 chats, which have no topic.

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


@mcp.tool()
async def list_chat_messages_page(chat_id: str, since: str = "", cursor: str = "") -> dict:
    """
    Fetch ONE page of a chat's messages as structured JSON.

    For programmatic clients. Messages come back newest-first and flattened:
    id, message_type, from_user_id, from_user_display, from_application_id,
    body_content, body_content_type, created, last_modified. System events have
    no sender, so the from_* fields are null. Returns next_cursor for the next
    page, empty when there are no more.

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

    return {
        "messages": [_chat_message_json(m) for m in data.get("value", [])],
        "next_cursor": data.get("@odata.nextLink", ""),
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


@mcp.tool()
async def send_chat_message_json(chat_id: str, text: str) -> dict:
    """
    Send a plain-text message to a Teams chat. Returns structured JSON.

    The content type is explicitly "text", not "auto": the desktop composer is
    a plain-text field, and auto-detection would read a typed "<" as markup —
    the same rule update_draft_body holds for mail. Returns the created message
    flattened exactly as list_chat_messages_page returns one, so the client can
    store its own reply without waiting for the next pull. Requires
    Chat.ReadWrite.

    On failure message is null and error says why ("teams_unavailable" when the
    account has no Teams license).

    Args:
        chat_id: The chat ID (from list_chats_page).
        text: The message body, sent as typed.
    """
    if not chat_id.strip():
        return {"message": None, "error": "chat_id must not be empty"}
    if not text.strip():
        return {"message": None, "error": "text must not be empty"}

    try:
        token = get_graph_token()
        async with AsyncGraphClient(token) as client:
            created = await teams_ops.asend_chat_message(client, chat_id, text, content_type="text")
    except PermissionError as e:
        return _not_connected(e)
    except TeamsNotAvailableError:
        return {"message": None, "error": "teams_unavailable"}

    return {"message": _chat_message_json(created)}


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

    return {
        "connected": True,
        "scopes": _stored_graph_scopes(),
        "connect_url": None,
        "account": account,
    }


if __name__ == "__main__":
    mcp.run()
