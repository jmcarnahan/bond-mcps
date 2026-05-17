#!/usr/bin/env python3
"""
Atlassian MCP Server for Bond AI.

Provides Jira, Confluence, and user tools that use the user's Atlassian OAuth
token and cloud ID, passed through by Bond AI's backend as HTTP headers:
  - Authorization: Bearer {token}
  - X-Atlassian-Cloud-Id: {cloud_id}

Run:
    fastmcp run atlassian_mcp.py --transport streamable-http --port 9001

Tool summary (5 tools):
  Jira       : jira_search, jira_get, jira_manage
  Confluence : confluence_search, confluence_manage
"""

import csv
import io
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional, Sequence

from fastmcp import FastMCP

from atlassian.auth import get_atlassian_token, get_cloud_id
from atlassian.atlassian_client import AsyncAtlassianClient, AtlassianError
from atlassian import jira as jira_ops
from atlassian import confluence as confluence_ops
from atlassian import user as user_ops

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app):
    """Warn if auth proxy is unreachable when local auth is configured."""
    if os.environ.get("ATLASSIAN_CLIENT_ID"):
        from auth import OAuthProxyClient
        proxy = OAuthProxyClient()
        try:
            proxy.check_proxy()
            logger.info("Auth proxy validated for local Atlassian auth")
        except RuntimeError as e:
            logger.warning("Auth proxy not available: %s", e)
    yield


mcp = FastMCP("Atlassian MCP Server", lifespan=_lifespan)


def _format_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Format rows as pipe-delimited CSV using Python's csv module for safe quoting."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().rstrip("\r\n")


def _friendly_error(err: AtlassianError, context: str = "") -> str:
    """Convert an AtlassianError into a user-friendly message."""
    code = err.error_code

    if code == "Unauthorized":
        return (
            "Atlassian authentication failed. Your session may have expired.\n"
            "Please reconnect your Atlassian account in Settings -> Connections."
        )
    if code == "Forbidden":
        resource = context if context else "this resource"
        return (
            f"You don't have permission to access {resource}. "
            "Check your Atlassian permissions."
        )
    if code == "NotFound":
        resource = context if context else "The requested resource"
        return f"{resource} was not found. Check the identifier and your access permissions."
    if code == "RateLimited":
        return f"Rate limited by Atlassian. Please wait and try again.\n({err})"
    if code == "BadRequest" or code == "InvalidTransition":
        return f"Atlassian rejected the request: {err}"
    if code == "Conflict":
        return f"Conflict — the resource may have been modified. Please refresh and try again.\n({err})"
    return f"Atlassian API error: {err}"


def _get_client() -> tuple:
    """Extract token and cloud_id, return (token, cloud_id)."""
    return get_atlassian_token(), get_cloud_id()


def _extract_adf_text(adf: dict) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if adf.get("type") == "text":
        return adf.get("text", "")

    children = adf.get("content", [])
    child_texts = [_extract_adf_text(c) for c in children]

    node_type = adf.get("type", "")
    if node_type in ("doc", "table", "tableRow", "bulletList", "orderedList"):
        return "\n".join(t for t in child_texts if t)

    if node_type in ("paragraph", "heading", "listItem", "blockquote",
                      "codeBlock", "tableCell", "tableHeader"):
        return "".join(child_texts)

    return "".join(child_texts)


def _format_issue_details(issue: dict, comments: list) -> str:
    """Format a full issue with comments into a readable string."""
    fields = issue.get("fields", {})
    rendered = issue.get("renderedFields", {})

    status = fields.get("status", {}).get("name", "?")
    priority = fields.get("priority", {}).get("name", "None") if fields.get("priority") else "None"
    issue_type = fields.get("issuetype", {}).get("name", "?")
    assignee = fields.get("assignee")
    assignee_str = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
    reporter = fields.get("reporter")
    reporter_str = reporter.get("displayName", "?") if reporter else "?"
    labels = ", ".join(fields.get("labels", []))
    created = fields.get("created", "?")
    updated = fields.get("updated", "?")

    lines = [
        f"**{issue.get('key', '?')} — {fields.get('summary', '?')}**",
        f"**Status:** {status}",
        f"**Type:** {issue_type} | **Priority:** {priority}",
        f"**Assignee:** {assignee_str} | **Reporter:** {reporter_str}",
        f"**Created:** {created} | **Updated:** {updated}",
    ]
    if labels:
        lines.append(f"**Labels:** {labels}")

    description = rendered.get("description") or ""
    if not description:
        desc_field = fields.get("description")
        if desc_field and isinstance(desc_field, dict):
            description = _extract_adf_text(desc_field)
        elif desc_field:
            description = str(desc_field)

    if description:
        lines.append(f"\n---\n**Description:**\n{description}")
    else:
        lines.append("\n---\n*(No description)*")

    if comments:
        lines.append(f"\n---\n**Comments ({len(comments)}):**\n")
        for c in comments:
            author = c.get("author", {}).get("displayName", "?")
            created_at = c.get("created", "?")
            body = ""
            body_field = c.get("body")
            if body_field and isinstance(body_field, dict):
                body = _extract_adf_text(body_field)
            elif body_field:
                body = str(body_field)
            lines.append(f"**{author}** ({created_at}):\n{body}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Jira tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def jira_search(
    target: str,
    jql: str = "",
    query: str = "",
    project_key: str = "",
    fields: str = "",
    status: str = "",
    max_results: int = 0,
) -> str:
    """Search and list Jira resources.

    Modes (set target):
    - "projects": List accessible Jira projects.
    - "issues": Search issues using JQL. Requires jql. Auto-paginates all results.
    - "issue_count": Count issues matching JQL without fetching data. Requires jql.
    - "versions": List release versions for a project. Requires project_key. Use query to filter by name pattern, status to filter by "released"/"unreleased".
    - "users": Search users by name/email to find account IDs for assignment or @mentions. Requires query.
    - "myself": Get current authenticated user's account ID, email, and display name.

    Args:
        target: What to search for (see modes above).
        jql: JQL query string (for issues/issue_count). Examples: "project = PROJ AND status = Open", "assignee = currentUser() ORDER BY updated DESC", "labels = release AND fixVersion = 1.2.0".
        query: Search term — name/email for users (e.g., "alice"), name pattern for versions (e.g., "1.2").
        project_key: Project key, e.g. "PROJ" (for versions).
        fields: Issue field set: "" (minimal: key,summary,status,type), "extended" (adds assignee,priority,created,updated,labels), or comma-separated field names.
        status: Version status filter: "released", "unreleased", or "" for all.
        max_results: Max results. 0 = target-specific default (projects:50, issues:all, spaces:25, users:10, versions:50).
    """
    _valid = ("projects", "issues", "issue_count", "versions", "users", "myself")
    if target not in _valid:
        return f"Invalid target '{target}'. Must be one of: {', '.join(_valid)}."

    token, cloud_id = _get_client()

    if target == "projects":
        limit = max_results if max_results > 0 else 50
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                projects = await jira_ops.alist_projects(client, max_results=limit)
        except AtlassianError as e:
            return _friendly_error(e)
        if not projects:
            return "No Jira projects found."
        rows = [[p.get("key", "?"), p.get("name", "?"), p.get("projectTypeKey", "?")] for p in projects]
        return f"{len(projects)} project(s):\n" + _format_table(["key", "name", "type"], rows)

    elif target == "issues":
        if not jql:
            return "Parameter 'jql' is required for target='issues'."
        if fields == "extended":
            field_str = jira_ops._SEARCH_FIELDS_EXTENDED
        elif fields:
            field_str = fields
        else:
            field_str = jira_ops._SEARCH_FIELDS_MINIMAL
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                count = await jira_ops.acount_issues(client, jql=jql)
                cap = max_results if max_results > 0 else jira_ops._MAX_TOTAL_ISSUES
                issues = await jira_ops.asearch_all_issues(
                    client, jql=jql, fields=field_str, max_total=cap,
                )
        except AtlassianError as e:
            return _friendly_error(e)
        if not issues:
            return f"No issues found matching: `{jql}`"
        fetched = len(issues)
        rows = []
        for issue in issues:
            f = issue.get("fields", {})
            rows.append([
                issue.get("key", "?"),
                f.get("summary", "?"),
                f.get("status", {}).get("name", "?"),
                f.get("issuetype", {}).get("name", ""),
                f.get("assignee", {}).get("displayName", "") if f.get("assignee") else "",
                f.get("priority", {}).get("name", "") if f.get("priority") else "",
                ",".join(f.get("labels", [])),
            ])
        result = f"{fetched} of ~{count} issue(s) for `{jql}`:\n"
        result += _format_table(["key", "summary", "status", "type", "assignee", "priority", "labels"], rows)
        if fetched < count:
            result += f"\n({fetched} of ~{count} shown. Refine JQL or set max_results for more.)"
        return result

    elif target == "issue_count":
        if not jql:
            return "Parameter 'jql' is required for target='issue_count'."
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                count = await jira_ops.acount_issues(client, jql=jql)
        except AtlassianError as e:
            return _friendly_error(e)
        return f"Approximately {count} issue(s) match `{jql}`."

    elif target == "versions":
        if not project_key:
            return "Parameter 'project_key' is required for target='versions'."
        limit = max_results if max_results > 0 else 50
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                versions = await jira_ops.aget_project_versions(
                    client,
                    project_key=project_key,
                    query=query,
                    status=status,
                    max_results=limit,
                )
        except AtlassianError as e:
            return _friendly_error(e, context=f"project {project_key}")
        if not versions:
            filter_desc = ""
            if query:
                filter_desc += f" matching '{query}'"
            if status:
                filter_desc += f" with status '{status}'"
            return f"No versions found for {project_key}{filter_desc}."
        rows = [
            [v.get("id", "?"), v.get("name", "?"), "released" if v.get("released", False) else "unreleased", v.get("releaseDate", "")]
            for v in versions
        ]
        return f"{len(versions)} version(s) for {project_key}:\n" + _format_table(["id", "name", "status", "releaseDate"], rows)

    elif target == "users":
        if not query:
            return "Parameter 'query' is required for target='users'."
        limit = max_results if max_results > 0 else 10
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                users = await jira_ops.asearch_users(client, query=query, max_results=limit)
        except AtlassianError as e:
            return _friendly_error(e)
        if not users:
            return f"No users found matching '{query}'."
        rows = [[u.get("accountId", "?"), u.get("displayName", "?"), u.get("emailAddress", "")] for u in users]
        return f"{len(users)} user(s) matching '{query}':\n" + _format_table(["accountId", "displayName", "email"], rows)

    elif target == "myself":
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                user = await user_ops.aget_myself(client)
        except AtlassianError as e:
            return _friendly_error(e)
        lines = [
            f"**{user.get('displayName', '?')}**",
            f"**Account ID:** {user.get('accountId', '?')}",
            f"**Email:** {user.get('emailAddress', 'Not available')}",
            f"**Active:** {user.get('active', '?')}",
            f"**Time Zone:** {user.get('timeZone', '?')}",
        ]
        return "\n".join(lines)


@mcp.tool()
async def jira_get(
    target: str,
    issue_key: str = "",
) -> str:
    """Get detailed Jira information for a single issue.

    Modes (set target):
    - "issue": Full issue details (status, type, priority, assignee, reporter, labels, description) plus all comments. Requires issue_key.
    - "transitions": List available workflow transitions for an issue (e.g., "To Do", "In Progress", "Done"). Use before jira_manage(target="transition") to discover valid transition names. Requires issue_key.

    Args:
        target: What to get (see modes above).
        issue_key: The issue key (e.g., "PROJ-123").
    """
    _valid = ("issue", "transitions")
    if target not in _valid:
        return f"Invalid target '{target}'. Must be one of: {', '.join(_valid)}."

    if not issue_key:
        return "Parameter 'issue_key' is required."

    token, cloud_id = _get_client()

    if target == "issue":
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                issue = await jira_ops.aget_issue(client, issue_key)
                try:
                    comments = await jira_ops.aget_issue_comments(client, issue_key)
                except AtlassianError:
                    comments = []
        except AtlassianError as e:
            return _friendly_error(e, context=issue_key)
        return _format_issue_details(issue, comments)

    elif target == "transitions":
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                transitions = await jira_ops.aget_transitions(client, issue_key)
        except AtlassianError as e:
            return _friendly_error(e, context=issue_key)
        if not transitions:
            return f"No transitions available for {issue_key}."
        names = [t.get("name", "?") for t in transitions]
        return f"Transitions for {issue_key}: {', '.join(names)}"


@mcp.tool()
async def jira_manage(
    target: str,
    issue_key: str = "",
    project_key: str = "",
    summary: str = "",
    issue_type: str = "Task",
    description: str = "",
    assignee_id: str = "",
    priority: str = "",
    labels: str = "",
    body: str = "",
    author_label: str = "",
    transition_name: str = "",
    name: str = "",
    release_date: str = "",
    released: bool = False,
) -> str:
    """Create, update, or transition Jira resources.

    Modes (set target):
    - "create_issue": Create a new issue. Requires project_key, summary.
    - "update_issue": Update issue fields. Requires issue_key + at least one field to change.
    - "comment": Add comment to issue. Requires issue_key, body. To @mention users, include @{accountId} in body text (use jira_search target="users" to find account IDs). Set author_label to display an AI agent identity prefix like "🤖 AI Agent (Bond AI):".
    - "transition": Move issue to another workflow state (e.g., "In Progress", "Done"). Requires issue_key, transition_name. Use jira_get(target="transitions") first to see available transition names.
    - "create_version": Create a release version in a project's release board. Requires project_key, name.

    Args:
        target: Action to perform (see modes above).
        issue_key: Issue key, e.g. "PROJ-123" (for update_issue, comment, transition).
        project_key: Project key, e.g. "PROJ" (for create_issue, create_version).
        summary: Issue title (for create_issue, update_issue).
        issue_type: Issue type: Task, Bug, Story, Epic (for create_issue, default: Task).
        description: Plain text description (for create_issue, update_issue, create_version).
        assignee_id: Atlassian account ID (for create_issue, update_issue). Use jira_search(target="users") to find IDs.
        priority: Priority: Highest, High, Medium, Low, Lowest (for create_issue, update_issue).
        labels: Comma-separated labels (for create_issue, update_issue).
        body: Comment text (for comment). To mention users, include @{accountId} syntax, e.g. "Hi @{5b10ac8d82e05b22cc7d4ef5} please review this".
        author_label: Display name shown as comment author prefix (for comment). E.g. "AI Agent" renders as "🤖 AI Agent (Bond AI): <comment>". Leave empty to omit.
        transition_name: Workflow transition name, case-insensitive (for transition). E.g. "In Progress", "Done", "To Do".
        name: Version name, e.g. "1.2.0" or "Q4 Release" (for create_version).
        release_date: Release date in YYYY-MM-DD format, e.g. "2026-06-30" (for create_version).
        released: Whether the version is already released (for create_version, default: false).
    """
    _valid = ("create_issue", "update_issue", "comment", "transition", "create_version")
    if target not in _valid:
        return f"Invalid target '{target}'. Must be one of: {', '.join(_valid)}."

    token, cloud_id = _get_client()

    if target == "create_issue":
        if not project_key:
            return "Parameter 'project_key' is required for target='create_issue'."
        if not summary:
            return "Parameter 'summary' is required for target='create_issue'."
        label_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else None
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                result = await jira_ops.acreate_issue(
                    client,
                    project_key=project_key,
                    summary=summary,
                    issue_type=issue_type,
                    description=description or None,
                    assignee_id=assignee_id or None,
                    priority=priority or None,
                    labels=label_list,
                )
        except AtlassianError as e:
            return _friendly_error(e)
        key = result.get("key", "?")
        return f"Issue created: **{key}** — {summary}"

    elif target == "update_issue":
        if not issue_key:
            return "Parameter 'issue_key' is required for target='update_issue'."
        label_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else None
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                await jira_ops.aupdate_issue(
                    client,
                    issue_key=issue_key,
                    summary=summary or None,
                    description=description or None,
                    assignee_id=assignee_id or None,
                    priority=priority or None,
                    labels=label_list,
                )
        except AtlassianError as e:
            return _friendly_error(e, context=issue_key)
        return f"Issue **{issue_key}** updated successfully."

    elif target == "comment":
        if not issue_key:
            return "Parameter 'issue_key' is required for target='comment'."
        if not body:
            return "Parameter 'body' is required for target='comment'."
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                result = await jira_ops.aadd_issue_comment(
                    client, issue_key, body, author_label=author_label,
                )
        except AtlassianError as e:
            return _friendly_error(e, context=issue_key)
        comment_id = result.get("id", "?") if result else "?"
        return f"Comment added to {issue_key} (comment ID: {comment_id})."

    elif target == "transition":
        if not issue_key:
            return "Parameter 'issue_key' is required for target='transition'."
        if not transition_name:
            return "Parameter 'transition_name' is required for target='transition'."
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                matched_name = await jira_ops.atransition_issue(client, issue_key, transition_name)
        except AtlassianError as e:
            return _friendly_error(e, context=issue_key)
        return f"Issue {issue_key} transitioned to {matched_name}."

    elif target == "create_version":
        if not project_key:
            return "Parameter 'project_key' is required for target='create_version'."
        if not name:
            return "Parameter 'name' is required for target='create_version'."
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                result = await jira_ops.acreate_version(
                    client,
                    project_key=project_key,
                    name=name,
                    description=description,
                    release_date=release_date,
                    released=released,
                )
        except AtlassianError as e:
            return _friendly_error(e)
        version_id = result.get("id", "?") if result else "?"
        return f"Version {name} created in project {project_key} (ID: {version_id})."


# ---------------------------------------------------------------------------
# Confluence tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def confluence_search(
    target: str,
    query: str = "",
    page_id: str = "",
    max_results: int = 0,
) -> str:
    """Search and read Confluence content.

    Modes (set target):
    - "spaces": List accessible Confluence spaces (returns key, name, type, status).
    - "pages": Search pages/blogs using CQL (Confluence Query Language). Requires query. Returns page IDs needed for get/update operations.
    - "page": Get full page content including body in storage format. Requires page_id. Also returns version number needed for updates.

    Args:
        target: What to find (see modes above).
        query: CQL query string (for pages). Examples: 'type = page AND text ~ "release notes"', 'space = DEV AND title ~ "architecture"', 'type = page AND label = "api-docs"'.
        page_id: Numeric page ID (for page). Get this from the "pages" search results.
        max_results: Max results for spaces/pages. 0 = default (25).
    """
    _valid = ("spaces", "pages", "page")
    if target not in _valid:
        return f"Invalid target '{target}'. Must be one of: {', '.join(_valid)}."

    token, cloud_id = _get_client()

    if target == "spaces":
        limit = max_results if max_results > 0 else 25
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                spaces = await confluence_ops.alist_spaces(client, max_results=limit)
        except AtlassianError as e:
            return _friendly_error(e)
        if not spaces:
            return "No Confluence spaces found."
        rows = [[s.get("key", "?"), s.get("name", "?"), s.get("type", "?"), s.get("status", "?")] for s in spaces]
        return f"{len(spaces)} space(s):\n" + _format_table(["key", "name", "type", "status"], rows)

    elif target == "pages":
        if not query:
            return "Parameter 'query' is required for target='pages'."
        limit = max_results if max_results > 0 else 25
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                results = await confluence_ops.asearch_content(client, query=query, max_results=limit)
        except AtlassianError as e:
            return _friendly_error(e)
        if not results:
            return f"No content found matching: `{query}`"
        rows = [[r.get("id", "?"), r.get("title", "?"), r.get("type", "?"), r.get("status", "?")] for r in results]
        return f"{len(results)} result(s) for `{query}`:\n" + _format_table(["id", "title", "type", "status"], rows)

    elif target == "page":
        if not page_id:
            return "Parameter 'page_id' is required for target='page'."
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                page = await confluence_ops.aget_page(client, page_id)
        except AtlassianError as e:
            return _friendly_error(e, context=f"page {page_id}")

        title = page.get("title", "?")
        page_status = page.get("status", "?")
        version = page.get("version", {}).get("number", "?")
        space_id = page.get("spaceId", "?")
        created = page.get("createdAt", "?")

        body_content = ""
        page_body = page.get("body", {})
        if page_body:
            storage = page_body.get("storage", {})
            body_content = storage.get("value", "")

        lines = [
            f"**{title}**",
            f"**Page ID:** {page_id} | **Space ID:** {space_id}",
            f"**Status:** {page_status} | **Version:** {version}",
            f"**Created:** {created}",
        ]

        if body_content:
            lines.append(f"\n---\n{body_content}")
        else:
            lines.append("\n---\n*(No content)*")

        return "\n".join(lines)


@mcp.tool()
async def confluence_manage(
    target: str,
    space_id: str = "",
    title: str = "",
    body: str = "",
    parent_id: str = "",
    page_id: str = "",
    version_number: int = 0,
) -> str:
    """Create or update Confluence pages.

    Modes (set target):
    - "create_page": Create a new page. Requires space_id, title, body.
    - "update_page": Update an existing page's title and/or content. Requires page_id, title, body. Use confluence_search(target="page") first to get current content and version number.

    Args:
        target: Action to perform (see modes above).
        space_id: Space ID to create page in (for create_page). Get this from confluence_search(target="spaces").
        title: Page title (for create_page, update_page).
        body: Page body in Confluence storage format (XHTML). For simple text use "<p>Your text here</p>". For headings: "<h1>Title</h1><p>Content</p>". For lists: "<ul><li>Item</li></ul>".
        parent_id: Parent page ID to nest under (for create_page, optional).
        page_id: Page ID to update (for update_page). Get from search results or page URL.
        version_number: New version number — must be current version + 1 (for update_page). Pass 0 to auto-detect the correct version.
    """
    _valid = ("create_page", "update_page")
    if target not in _valid:
        return f"Invalid target '{target}'. Must be one of: {', '.join(_valid)}."

    token, cloud_id = _get_client()

    if target == "create_page":
        if not space_id:
            return "Parameter 'space_id' is required for target='create_page'."
        if not title:
            return "Parameter 'title' is required for target='create_page'."
        if not body:
            return "Parameter 'body' is required for target='create_page'."
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                result = await confluence_ops.acreate_page(
                    client,
                    space_id=space_id,
                    title=title,
                    body=body,
                    parent_id=parent_id or None,
                )
        except AtlassianError as e:
            return _friendly_error(e)
        created_id = result.get("id", "?")
        return f"Page created: **{title}** (ID: {created_id})"

    elif target == "update_page":
        if not page_id:
            return "Parameter 'page_id' is required for target='update_page'."
        if not title:
            return "Parameter 'title' is required for target='update_page'."
        if not body:
            return "Parameter 'body' is required for target='update_page'."
        try:
            async with AsyncAtlassianClient(token, cloud_id) as client:
                if version_number == 0:
                    page = await confluence_ops.aget_page(client, page_id)
                    current_version = page.get("version", {}).get("number", 0)
                    version_number = current_version + 1
                await confluence_ops.aupdate_page(
                    client,
                    page_id=page_id,
                    title=title,
                    body=body,
                    version_number=version_number,
                )
        except AtlassianError as e:
            return _friendly_error(e, context=f"page {page_id}")
        return f"Page **{title}** (ID: {page_id}) updated to version {version_number}."


if __name__ == "__main__":
    mcp.run()
