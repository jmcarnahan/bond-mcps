#!/usr/bin/env python3
"""
Atlassian CLI -- interact with Jira & Confluence using OAuth 2.0 authorization code flow.

Uses the shared OAuth callback proxy (auth) for browser-based auth.

Setup:
    1. Start the shared auth proxy:
       cd auth && poetry run python -m auth
    2. Set env vars:
       export ATLASSIAN_CLIENT_ID=<your-client-id>
       export ATLASSIAN_CLIENT_SECRET=<your-client-secret>

Usage:
    atlassian-cli jira projects
    atlassian-cli jira search "project = PROJ AND status = Open"
    atlassian-cli jira count "project = PROJ"
    atlassian-cli jira get PROJ-123
    atlassian-cli jira create PROJ "Fix the login bug" --type Bug --priority High
    atlassian-cli jira comment PROJ-123 "Working on this now"
    atlassian-cli jira transition PROJ-123 "In Progress"
    atlassian-cli jira transitions PROJ-123
    atlassian-cli jira versions PROJ [--query 1.2] [--status unreleased]
    atlassian-cli jira create-version PROJ "1.2.0" [--release-date 2026-06-30]
    atlassian-cli jira lookup-user "alice"

    atlassian-cli confluence spaces
    atlassian-cli confluence search 'type = page AND text ~ "release"'
    atlassian-cli confluence get 12345

    atlassian-cli user me

    atlassian-cli logout  # Clear cached tokens
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from auth import log_discipline  # noqa: E402
log_discipline.apply()

from atlassian.atlassian_client import AtlassianClient, AtlassianError
from atlassian.local_auth import get_local_token_and_cloud_id
from atlassian import jira as jira_ops
from atlassian import confluence as confluence_ops
from atlassian import user as user_ops

from auth import TokenStore


def _get_token_and_cloud_id() -> tuple[str, str]:
    """Get a valid access token and cloud_id, authenticating if needed."""
    # Allow direct token override for testing
    direct_token = os.environ.get("ATLASSIAN_ACCESS_TOKEN", "")
    direct_cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID", "")
    if direct_token and direct_cloud_id:
        return direct_token, direct_cloud_id

    return get_local_token_and_cloud_id()


def _get_client() -> AtlassianClient:
    """Get an authenticated AtlassianClient."""
    token, cloud_id = _get_token_and_cloud_id()
    return AtlassianClient(token, cloud_id)


def _pp(data):
    """Pretty-print JSON data."""
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Jira commands
# ---------------------------------------------------------------------------

def cmd_jira_projects(args):
    with _get_client() as client:
        projects = jira_ops.list_projects(client, max_results=args.max_results)
    if not projects:
        print("No projects found.")
        return
    print(f"Projects ({len(projects)}):\n")
    for p in projects:
        print(f"  {p.get('key', '?'):10s} {p.get('name', '?')} ({p.get('projectTypeKey', '?')})")


def cmd_jira_search(args):
    # Resolve field set
    if args.fields == "extended":
        field_str = jira_ops._SEARCH_FIELDS_EXTENDED
    elif args.fields:
        field_str = args.fields
    else:
        field_str = jira_ops._SEARCH_FIELDS_MINIMAL

    with _get_client() as client:
        # Count first
        count = jira_ops.count_issues(client, jql=args.jql)
        print(f"~{count} issue(s) match. Fetching...\n")

        # Fetch all or up to max_results
        cap = args.max_results if args.max_results > 0 else jira_ops._MAX_TOTAL_ISSUES
        issues = jira_ops.search_all_issues(
            client, jql=args.jql, fields=field_str, max_total=cap,
        )

    if not issues:
        print("No issues found.")
        return

    print(f"Issues ({len(issues)} of ~{count}):\n")
    for issue in issues:
        f = issue.get("fields", {})
        key = issue.get("key", "?")
        summary = f.get("summary", "?")
        status = f.get("status", {}).get("name", "?")

        extras = []
        if f.get("assignee"):
            extras.append(f.get("assignee", {}).get("displayName", "?"))
        if f.get("priority"):
            extras.append(f.get("priority", {}).get("name", "?"))

        extra_str = f"  ({', '.join(extras)})" if extras else ""
        print(f"  {key:12s} [{status:15s}] {summary}{extra_str}")

    if len(issues) < count:
        print(f"\n  (Showing {len(issues)} of ~{count}. Use --max-results or refine JQL.)")


def cmd_jira_count(args):
    with _get_client() as client:
        count = jira_ops.count_issues(client, jql=args.jql)
    print(f"Approximate count: {count}")


def cmd_jira_get(args):
    with _get_client() as client:
        issue = jira_ops.get_issue(client, args.issue_key)
    fields = issue.get("fields", {})
    print(f"\n{issue.get('key', '?')} — {fields.get('summary', '?')}")
    print(f"  Status: {fields.get('status', {}).get('name', '?')}")
    print(f"  Type: {fields.get('issuetype', {}).get('name', '?')}")
    priority = fields.get("priority")
    print(f"  Priority: {priority.get('name', '?') if priority else 'None'}")
    assignee = fields.get("assignee")
    print(f"  Assignee: {assignee.get('displayName', '?') if assignee else 'Unassigned'}")
    reporter = fields.get("reporter")
    print(f"  Reporter: {reporter.get('displayName', '?') if reporter else '?'}")
    print(f"  Created: {fields.get('created', '?')}")
    print(f"  Updated: {fields.get('updated', '?')}")
    labels = fields.get("labels", [])
    if labels:
        print(f"  Labels: {', '.join(labels)}")
    desc = issue.get("renderedFields", {}).get("description", "")
    if desc:
        truncated = desc[:500] + ("... (truncated)" if len(desc) > 500 else "")
        print(f"\n  Description:\n  {truncated}")


def cmd_jira_create(args):
    labels = [l.strip() for l in args.labels.split(",") if l.strip()] if args.labels else None
    with _get_client() as client:
        result = jira_ops.create_issue(
            client,
            project_key=args.project_key,
            summary=args.summary,
            issue_type=args.type,
            description=args.description or None,
            priority=args.priority or None,
            labels=labels,
        )
    print(f"Created: {result.get('key', '?')}")


def cmd_jira_update(args):
    kwargs = {}
    if args.summary:
        kwargs["summary"] = args.summary
    if args.description:
        kwargs["description"] = args.description
    if args.priority:
        kwargs["priority"] = args.priority
    if args.labels:
        kwargs["labels"] = [l.strip() for l in args.labels.split(",") if l.strip()]
    if args.assignee:
        kwargs["assignee_id"] = args.assignee
    if not kwargs:
        print("Nothing to update — provide at least one of --summary, --description, --priority, --labels, --assignee")
        return
    with _get_client() as client:
        jira_ops.update_issue(client, args.issue_key, **kwargs)
    print(f"Updated {args.issue_key}")


def cmd_jira_comment(args):
    with _get_client() as client:
        result = jira_ops.add_issue_comment(client, args.issue_key, args.body)
    print(f"Comment added (ID: {result.get('id', '?')})")


def cmd_jira_transition(args):
    with _get_client() as client:
        matched = jira_ops.transition_issue(client, args.issue_key, args.transition_name)
    print(f"Transitioned {args.issue_key} to '{matched}'")


def cmd_jira_transitions(args):
    with _get_client() as client:
        transitions = jira_ops.get_transitions(client, args.issue_key)
    if not transitions:
        print(f"No transitions available for {args.issue_key}.")
        return
    print(f"Available transitions for {args.issue_key}:\n")
    for t in transitions:
        print(f"  {t.get('name', '?')}")


def cmd_jira_versions(args):
    with _get_client() as client:
        versions = jira_ops.get_project_versions(
            client,
            project_key=args.project_key,
            query=args.query,
            status=args.status,
            max_results=args.max_results,
        )
    if not versions:
        print(f"No versions found for {args.project_key}.")
        return
    print(f"Versions for {args.project_key} ({len(versions)}):\n")
    for v in versions:
        released = v.get("released", False)
        status_str = "Released" if released else "Unreleased"
        release_date = v.get("releaseDate", "")
        date_str = f"  release: {release_date}" if release_date else ""
        print(f"  {v.get('name', '?'):20s} [{status_str:10s}]{date_str}  (ID: {v.get('id', '?')})")


def cmd_jira_create_version(args):
    with _get_client() as client:
        result = jira_ops.create_version(
            client,
            project_key=args.project_key,
            name=args.name,
            description=args.description,
            release_date=args.release_date,
            released=args.released,
        )
    print(f"Version created: {result.get('name', '?')} (ID: {result.get('id', '?')})")


def cmd_jira_lookup_user(args):
    with _get_client() as client:
        users = jira_ops.search_users(client, query=args.query, max_results=args.max_results)
    if not users:
        print(f"No users found matching '{args.query}'.")
        return
    print(f"Users matching '{args.query}':\n")
    for u in users:
        email = u.get("emailAddress", "")
        email_str = f"  <{email}>" if email else ""
        print(f"  {u.get('displayName', '?'):30s}{email_str}  accountId: {u.get('accountId', '?')}")


# ---------------------------------------------------------------------------
# Confluence commands
# ---------------------------------------------------------------------------

def cmd_confluence_spaces(args):
    with _get_client() as client:
        spaces = confluence_ops.list_spaces(client, max_results=args.max_results)
    if not spaces:
        print("No spaces found.")
        return
    print(f"Spaces ({len(spaces)}):\n")
    for s in spaces:
        print(f"  {s.get('key', '?'):10s} {s.get('name', '?')} ({s.get('type', '?')})")


def cmd_confluence_search(args):
    with _get_client() as client:
        results = confluence_ops.search_content(client, query=args.query, max_results=args.max_results)
    if not results:
        print("No results found.")
        return
    print(f"Results ({len(results)}):\n")
    for r in results:
        title = r.get("title", "?")
        page_id = r.get("id", "?")
        ctype = r.get("type", "?")
        print(f"  {page_id:10s} [{ctype:8s}] {title}")


def cmd_confluence_get(args):
    with _get_client() as client:
        page = confluence_ops.get_page(client, args.page_id)
    print(f"\n{page.get('title', '?')}")
    print(f"  Page ID: {page.get('id', '?')}")
    print(f"  Space ID: {page.get('spaceId', '?')}")
    print(f"  Version: {page.get('version', {}).get('number', '?')}")
    print(f"  Status: {page.get('status', '?')}")
    body = page.get("body", {}).get("storage", {}).get("value", "")
    if body:
        print(f"\n  Content:\n  {body[:1000]}")


# ---------------------------------------------------------------------------
# User commands
# ---------------------------------------------------------------------------

def cmd_user_me(args):
    with _get_client() as client:
        user = user_ops.get_myself(client)
    print(f"\n{user.get('displayName', '?')}")
    print(f"  Account ID: {user.get('accountId', '?')}")
    print(f"  Email: {user.get('emailAddress', 'Not available')}")
    print(f"  Active: {user.get('active', '?')}")
    print(f"  Time Zone: {user.get('timeZone', '?')}")


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

def cmd_logout(args):
    # TokenStore is DB-backed now — the file-based `cache_file` attribute was
    # removed in PR #4 (db-backed-token-cache). Use get_token() which returns
    # None when nothing is stored.
    store = TokenStore("atlassian")
    if store.get_token() is not None:
        store.clear()
        print("Logged out. Cached tokens removed.")
    else:
        print("No cached tokens found.")


# ---------------------------------------------------------------------------
# Raw MCP-mirror commands (for testing consolidated tools)
# ---------------------------------------------------------------------------

def cmd_raw_jira_search(args):
    import asyncio
    from atlassian_mcp import jira_search
    result = asyncio.run(jira_search(
        target=args.target,
        jql=args.jql,
        query=args.query,
        project_key=args.project_key,
        fields=args.fields,
        status=args.status,
        max_results=args.max_results,
    ))
    print(result)


def cmd_raw_jira_get(args):
    import asyncio
    from atlassian_mcp import jira_get
    result = asyncio.run(jira_get(
        target=args.target,
        issue_key=args.issue_key,
    ))
    print(result)


def cmd_raw_jira_manage(args):
    import asyncio
    from atlassian_mcp import jira_manage
    result = asyncio.run(jira_manage(
        target=args.target,
        issue_key=args.issue_key,
        project_key=args.project_key,
        summary=args.summary,
        issue_type=args.issue_type,
        description=args.description,
        assignee_id=args.assignee_id,
        priority=args.priority,
        labels=args.labels,
        body=args.body,
        author_label=args.author_label,
        transition_name=args.transition_name,
        name=args.name,
        release_date=args.release_date,
        released=args.released,
    ))
    print(result)


def cmd_raw_confluence_search(args):
    import asyncio
    from atlassian_mcp import confluence_search
    result = asyncio.run(confluence_search(
        target=args.target,
        query=args.query,
        page_id=args.page_id,
        max_results=args.max_results,
    ))
    print(result)


def cmd_raw_confluence_manage(args):
    import asyncio
    from atlassian_mcp import confluence_manage
    result = asyncio.run(confluence_manage(
        target=args.target,
        space_id=args.space_id,
        title=args.title,
        body=args.body,
        parent_id=args.parent_id,
        page_id=args.page_id,
        version_number=args.version_number,
    ))
    print(result)


# ---------------------------------------------------------------------------
# CLI setup
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Atlassian CLI — Jira & Confluence with OAuth 2.0 login"
    )
    subparsers = parser.add_subparsers(dest="service", required=True)

    # --- Jira ---
    jira_parser = subparsers.add_parser("jira", help="Jira operations")
    jira_sub = jira_parser.add_subparsers(dest="command", required=True)

    p = jira_sub.add_parser("projects", help="List projects")
    p.add_argument("--max-results", type=int, default=50)
    p.set_defaults(func=cmd_jira_projects)

    p = jira_sub.add_parser("search", help="Search issues with JQL")
    p.add_argument("jql", help="JQL query string")
    p.add_argument("--max-results", type=int, default=0,
                   help="Max issues to return (0 = all, default: all)")
    p.add_argument("--fields", default="",
                   help='Fields to include. "extended" for full set, or comma-separated list (default: minimal)')
    p.set_defaults(func=cmd_jira_search)

    p = jira_sub.add_parser("count", help="Count issues matching JQL")
    p.add_argument("jql", help="JQL query string")
    p.set_defaults(func=cmd_jira_count)

    p = jira_sub.add_parser("get", help="Get issue details")
    p.add_argument("issue_key", help="Issue key (e.g., PROJ-123)")
    p.set_defaults(func=cmd_jira_get)

    p = jira_sub.add_parser("create", help="Create an issue")
    p.add_argument("project_key", help="Project key (e.g., PROJ)")
    p.add_argument("summary", help="Issue summary")
    p.add_argument("--type", default="Task", help="Issue type (default: Task)")
    p.add_argument("--description", default="", help="Issue description")
    p.add_argument("--priority", default="", help="Priority name")
    p.add_argument("--labels", default="", help="Comma-separated labels")
    p.set_defaults(func=cmd_jira_create)

    p = jira_sub.add_parser("update", help="Update an issue")
    p.add_argument("issue_key", help="Issue key (e.g., PROJ-123)")
    p.add_argument("--summary", default="", help="New summary")
    p.add_argument("--description", default="", help="New description")
    p.add_argument("--priority", default="", help="New priority name")
    p.add_argument("--labels", default="", help="Comma-separated labels")
    p.add_argument("--assignee", default="", help="Assignee account ID")
    p.set_defaults(func=cmd_jira_update)

    p = jira_sub.add_parser("comment", help="Add comment to issue")
    p.add_argument("issue_key", help="Issue key (e.g., PROJ-123)")
    p.add_argument("body", help="Comment text")
    p.set_defaults(func=cmd_jira_comment)

    p = jira_sub.add_parser("transition", help="Transition an issue")
    p.add_argument("issue_key", help="Issue key (e.g., PROJ-123)")
    p.add_argument("transition_name", help="Transition name (e.g., 'In Progress')")
    p.set_defaults(func=cmd_jira_transition)

    p = jira_sub.add_parser("transitions", help="List available transitions for an issue")
    p.add_argument("issue_key", help="Issue key (e.g., PROJ-123)")
    p.set_defaults(func=cmd_jira_transitions)

    p = jira_sub.add_parser("versions", help="List project versions/releases")
    p.add_argument("project_key", help="Project key (e.g., PROJ)")
    p.add_argument("--query", default="", help="Filter by name pattern")
    p.add_argument("--status", default="", choices=["released", "unreleased", ""],
                   help="Filter by status (default: all)")
    p.add_argument("--max-results", type=int, default=50)
    p.set_defaults(func=cmd_jira_versions)

    p = jira_sub.add_parser("create-version", help="Create a project version/release")
    p.add_argument("project_key", help="Project key (e.g., PROJ)")
    p.add_argument("name", help="Version name (e.g., '1.2.0')")
    p.add_argument("--description", default="", help="Version description")
    p.add_argument("--release-date", default="", help="Release date in YYYY-MM-DD format")
    p.add_argument("--released", action="store_true", help="Mark as already released")
    p.set_defaults(func=cmd_jira_create_version)

    p = jira_sub.add_parser("lookup-user", help="Search for users by name or email")
    p.add_argument("query", help="Name or email to search for")
    p.add_argument("--max-results", type=int, default=10)
    p.set_defaults(func=cmd_jira_lookup_user)

    # --- Confluence ---
    conf_parser = subparsers.add_parser("confluence", help="Confluence operations")
    conf_sub = conf_parser.add_subparsers(dest="command", required=True)

    p = conf_sub.add_parser("spaces", help="List spaces")
    p.add_argument("--max-results", type=int, default=25)
    p.set_defaults(func=cmd_confluence_spaces)

    p = conf_sub.add_parser("search", help="Search content with CQL")
    p.add_argument("query", help="CQL query string")
    p.add_argument("--max-results", type=int, default=25)
    p.set_defaults(func=cmd_confluence_search)

    p = conf_sub.add_parser("get", help="Get page details")
    p.add_argument("page_id", help="Page ID")
    p.set_defaults(func=cmd_confluence_get)

    # --- User ---
    user_parser = subparsers.add_parser("user", help="User operations")
    user_sub = user_parser.add_subparsers(dest="command", required=True)

    p = user_sub.add_parser("me", help="Get current user info")
    p.set_defaults(func=cmd_user_me)

    # --- Raw (MCP-mirror for testing) ---
    raw_parser = subparsers.add_parser("raw", help="Raw MCP tool interface (for testing)")
    raw_sub = raw_parser.add_subparsers(dest="command", required=True)

    p = raw_sub.add_parser("jira-search", help="Mirror of jira_search MCP tool")
    p.add_argument("--target", required=True,
                   choices=["projects", "issues", "issue_count", "versions", "users", "myself"])
    p.add_argument("--jql", default="")
    p.add_argument("--query", default="")
    p.add_argument("--project-key", default="")
    p.add_argument("--fields", default="")
    p.add_argument("--status", default="")
    p.add_argument("--max-results", type=int, default=0)
    p.set_defaults(func=cmd_raw_jira_search)

    p = raw_sub.add_parser("jira-get", help="Mirror of jira_get MCP tool")
    p.add_argument("--target", required=True, choices=["issue", "transitions"])
    p.add_argument("--issue-key", default="")
    p.set_defaults(func=cmd_raw_jira_get)

    p = raw_sub.add_parser("jira-manage", help="Mirror of jira_manage MCP tool")
    p.add_argument("--target", required=True,
                   choices=["create_issue", "update_issue", "comment", "transition", "create_version"])
    p.add_argument("--issue-key", default="")
    p.add_argument("--project-key", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--issue-type", default="Task")
    p.add_argument("--description", default="")
    p.add_argument("--assignee-id", default="")
    p.add_argument("--priority", default="")
    p.add_argument("--labels", default="")
    p.add_argument("--body", default="")
    p.add_argument("--author-label", default="")
    p.add_argument("--transition-name", default="")
    p.add_argument("--name", default="")
    p.add_argument("--release-date", default="")
    p.add_argument("--released", action="store_true")
    p.set_defaults(func=cmd_raw_jira_manage)

    p = raw_sub.add_parser("confluence-search", help="Mirror of confluence_search MCP tool")
    p.add_argument("--target", required=True, choices=["spaces", "pages", "page"])
    p.add_argument("--query", default="")
    p.add_argument("--page-id", default="")
    p.add_argument("--max-results", type=int, default=0)
    p.set_defaults(func=cmd_raw_confluence_search)

    p = raw_sub.add_parser("confluence-manage", help="Mirror of confluence_manage MCP tool")
    p.add_argument("--target", required=True, choices=["create_page", "update_page"])
    p.add_argument("--space-id", default="")
    p.add_argument("--title", default="")
    p.add_argument("--body", default="")
    p.add_argument("--parent-id", default="")
    p.add_argument("--page-id", default="")
    p.add_argument("--version-number", type=int, default=0)
    p.set_defaults(func=cmd_raw_confluence_manage)

    # --- Logout ---
    logout_parser = subparsers.add_parser("logout", help="Clear cached tokens")
    logout_parser.set_defaults(func=cmd_logout)

    args = parser.parse_args()

    try:
        args.func(args)
    except AtlassianError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.")


if __name__ == "__main__":
    main()
