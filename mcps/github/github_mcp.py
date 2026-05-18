#!/usr/bin/env python3
"""
GitHub MCP Server.

Provides repository, issue, pull request, and code tools. Tokens are resolved
in this order (see github/auth.py):
  1. Authorization: Bearer header (backend mode, e.g. Bond AI)
  2. Local OAuth via github.local_auth (standalone mode — Claude Code, CLI),
     activated when GITHUB_CLIENT_ID is set; reads/writes ~/.bond_mcps/github.json

Run (standalone):
    make dev                                                                # all 4 services
    poetry run fastmcp run github_mcp.py --transport streamable-http --port 18002
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import JSONResponse

load_dotenv(Path(__file__).parent / ".env")

from github.auth import get_github_token
from github.github_client import AsyncGitHubClient, GitHubError
from github import repos as repos_ops
from github import issues as issues_ops
from github import pulls as pulls_ops
from github import code as code_ops

logging.basicConfig(level=logging.INFO)
from auth import log_discipline  # noqa: E402
log_discipline.apply()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app):
    """Fail fast on misconfig and warn if the auth proxy isn't reachable."""
    from auth import startup
    startup.verify_runtime_config()

    if os.environ.get("GITHUB_CLIENT_ID"):
        from auth import OAuthProxyClient
        proxy = OAuthProxyClient()
        try:
            proxy.check_proxy()
            logger.info("Auth proxy validated for local GitHub auth")
        except RuntimeError as e:
            logger.warning("Auth proxy not available: %s", e)
    yield


mcp = FastMCP("GitHub MCP Server", lifespan=_lifespan)


# Liveness/readiness probe. Returns 200 immediately if the ASGI app is up.
# Does NOT touch the DB or auth proxy — those are validated at startup by
# `bond-mcps doctor`. Used by k8s probes + the ALB target-group healthcheck.
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    return JSONResponse({"status": "ok"})


def _friendly_error(err: GitHubError) -> str:
    """Convert a GitHubError into a user-friendly message."""
    code = err.error_code
    if code == "BadCredentials":
        return (
            "GitHub authentication failed. Your token may have expired or been revoked.\n"
            "Please reconnect your GitHub account in Settings -> Connections."
        )
    if code == "RateLimitExceeded":
        return f"GitHub API rate limit exceeded. Please wait a few minutes and try again.\n({err})"
    if code == "NotFound":
        return (
            "The requested resource was not found on GitHub. "
            "Please check the owner, repository name, and issue/PR number are correct."
        )
    if code == "ValidationFailed":
        return f"GitHub rejected the request due to validation errors.\n{err}"
    return f"GitHub API error: {err}"


# ---------------------------------------------------------------------------
# Repository tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_repositories(type: str = "all", sort: str = "updated", per_page: int = 30) -> str:
    """
    List repositories for the authenticated GitHub user.

    Args:
        type: Filter by repo type - all, owner, public, private, member (default: all).
        sort: Sort by - created, updated, pushed, full_name (default: updated).
        per_page: Maximum number of repos to return (default: 30).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            repo_list = await repos_ops.alist_repos(client, type=type, sort=sort, per_page=per_page)
    except GitHubError as e:
        return _friendly_error(e)

    if not repo_list:
        return "No repositories found."

    lines = [f"Found {len(repo_list)} repository(ies):\n"]
    for r in repo_list:
        visibility = "private" if r.get("private") else "public"
        lang = r.get("language") or "—"
        stars = r.get("stargazers_count", 0)
        desc = r.get("description") or ""
        lines.append(
            f"- **{r.get('full_name', '?')}** ({visibility}, {lang}, {stars} stars)\n"
            f"  {desc}"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def get_repository(owner: str, repo: str) -> str:
    """
    Get detailed information about a GitHub repository.

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            r = await repos_ops.aget_repo(client, owner, repo)
    except GitHubError as e:
        return _friendly_error(e)

    visibility = "Private" if r.get("private") else "Public"
    lang = r.get("language") or "Not specified"
    return (
        f"**{r.get('full_name', '?')}**\n"
        f"**Description:** {r.get('description') or 'None'}\n"
        f"**Visibility:** {visibility}\n"
        f"**Language:** {lang}\n"
        f"**Stars:** {r.get('stargazers_count', 0)} | **Forks:** {r.get('forks_count', 0)} | **Open Issues:** {r.get('open_issues_count', 0)}\n"
        f"**Default Branch:** {r.get('default_branch', '?')}\n"
        f"**Created:** {r.get('created_at', '?')}\n"
        f"**Updated:** {r.get('updated_at', '?')}\n"
        f"**URL:** {r.get('html_url', '?')}"
    )


@mcp.tool()
async def search_repositories(query: str, sort: str = "best-match", per_page: int = 10) -> str:
    """
    Search for repositories across GitHub.

    Args:
        query: Search query (e.g., "machine learning language:python", "tetris stars:>100").
        sort: Sort by - best-match, stars, forks, updated (default: best-match).
        per_page: Maximum number of results (default: 10).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            results = await repos_ops.asearch_repos(client, query=query, sort=sort, per_page=per_page)
    except GitHubError as e:
        return _friendly_error(e)

    if not results:
        return f'No repositories found matching "{query}".'

    lines = [f'Found {len(results)} repository(ies) matching "{query}":\n']
    for r in results:
        lang = r.get("language") or "—"
        stars = r.get("stargazers_count", 0)
        desc = r.get("description") or ""
        lines.append(
            f"- **{r.get('full_name', '?')}** ({lang}, {stars} stars)\n"
            f"  {desc}\n"
            f"  {r.get('html_url', '')}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Issue tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_issues(owner: str, repo: str, state: str = "open", labels: str = "", per_page: int = 30) -> str:
    """
    List issues in a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        state: Filter by state - open, closed, all (default: open).
        labels: Comma-separated list of label names to filter by (optional).
        per_page: Maximum number of issues to return (default: 30).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            issue_list = await issues_ops.alist_issues(
                client, owner, repo, state=state, labels=labels, per_page=per_page
            )
    except GitHubError as e:
        return _friendly_error(e)

    # Filter out pull requests (GitHub issues API includes PRs)
    issue_list = [i for i in issue_list if "pull_request" not in i]

    if not issue_list:
        return f"No {state} issues found in {owner}/{repo}."

    lines = [f"Found {len(issue_list)} {state} issue(s) in {owner}/{repo}:\n"]
    for issue in issue_list:
        issue_labels = ", ".join(l.get("name", "?") for l in issue.get("labels", []))
        label_str = f" [{issue_labels}]" if issue_labels else ""
        assignee = issue.get("assignee")
        assignee_str = f" -> @{assignee['login']}" if assignee else ""
        lines.append(
            f"- **#{issue.get('number', '?')}** {issue.get('title', '?')}{label_str}{assignee_str}\n"
            f"  Created: {issue.get('created_at', '?')} | Comments: {issue.get('comments', 0)}"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def get_issue(owner: str, repo: str, issue_number: int) -> str:
    """
    Get detailed information about a specific issue, including comments.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: The issue number.
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            issue = await issues_ops.aget_issue(client, owner, repo, issue_number)
            comments = await issues_ops.aget_issue_comments(client, owner, repo, issue_number)
    except GitHubError as e:
        return _friendly_error(e)

    issue_labels = ", ".join(l.get("name", "?") for l in issue.get("labels", []))
    assignees = ", ".join(f"@{a['login']}" for a in issue.get("assignees", []))

    lines = [
        f"**#{issue.get('number', '?')} {issue.get('title', '?')}**",
        f"**State:** {issue.get('state', '?')}",
        f"**Author:** @{issue.get('user', {}).get('login', '?')}",
        f"**Created:** {issue.get('created_at', '?')}",
        f"**Updated:** {issue.get('updated_at', '?')}",
    ]
    if issue_labels:
        lines.append(f"**Labels:** {issue_labels}")
    if assignees:
        lines.append(f"**Assignees:** {assignees}")
    lines.append(f"**URL:** {issue.get('html_url', '?')}")

    body = issue.get("body") or "(no description)"
    lines.append(f"\n---\n{body}")

    if comments:
        lines.append(f"\n---\n**Comments ({len(comments)}):**\n")
        for c in comments:
            lines.append(
                f"**@{c.get('user', {}).get('login', '?')}** ({c.get('created_at', '?')}):\n"
                f"{c.get('body', '')}\n"
            )

    return "\n".join(lines)


@mcp.tool()
async def create_issue(owner: str, repo: str, title: str, body: str = "", labels: str = "", assignees: str = "") -> str:
    """
    Create a new issue in a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        title: Issue title.
        body: Issue body/description (optional).
        labels: Comma-separated label names (optional).
        assignees: Comma-separated usernames to assign (optional).
    """
    token = get_github_token()
    label_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else None
    assignee_list = [a.strip() for a in assignees.split(",") if a.strip()] if assignees else None

    try:
        async with AsyncGitHubClient(token) as client:
            issue = await issues_ops.acreate_issue(
                client, owner, repo, title=title, body=body,
                labels=label_list, assignees=assignee_list,
            )
    except GitHubError as e:
        return _friendly_error(e)

    return (
        f"Issue created: **#{issue.get('number', '?')}** {issue.get('title', '?')}\n"
        f"URL: {issue.get('html_url', '?')}"
    )


@mcp.tool()
async def update_issue(
    owner: str, repo: str, issue_number: int,
    title: str = "", body: str = "", state: str = "", labels: str = "",
) -> str:
    """
    Update an existing issue.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: The issue number to update.
        title: New title (leave empty to keep current).
        body: New body (leave empty to keep current).
        state: New state - open or closed (leave empty to keep current).
        labels: Comma-separated label names to set (leave empty to keep current).
    """
    token = get_github_token()
    label_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else None

    try:
        async with AsyncGitHubClient(token) as client:
            issue = await issues_ops.aupdate_issue(
                client, owner, repo, issue_number,
                title=title or None, body=body or None,
                state=state or None, labels=label_list,
            )
    except GitHubError as e:
        return _friendly_error(e)

    return (
        f"Issue updated: **#{issue.get('number', '?')}** {issue.get('title', '?')}\n"
        f"State: {issue.get('state', '?')}\n"
        f"URL: {issue.get('html_url', '?')}"
    )


@mcp.tool()
async def add_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> str:
    """
    Add a comment to an issue.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: The issue number to comment on.
        body: Comment text.
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            comment = await issues_ops.aadd_issue_comment(client, owner, repo, issue_number, body)
    except GitHubError as e:
        return _friendly_error(e)

    return (
        f"Comment added to issue #{issue_number}.\n"
        f"URL: {comment.get('html_url', '?')}"
    )


# ---------------------------------------------------------------------------
# Pull request tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_pull_requests(owner: str, repo: str, state: str = "open", sort: str = "created", per_page: int = 30) -> str:
    """
    List pull requests in a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        state: Filter by state - open, closed, all (default: open).
        sort: Sort by - created, updated, popularity, long-running (default: created).
        per_page: Maximum number of PRs to return (default: 30).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            pr_list = await pulls_ops.alist_pulls(
                client, owner, repo, state=state, sort=sort, per_page=per_page
            )
    except GitHubError as e:
        return _friendly_error(e)

    if not pr_list:
        return f"No {state} pull requests found in {owner}/{repo}."

    lines = [f"Found {len(pr_list)} {state} pull request(s) in {owner}/{repo}:\n"]
    for pr in pr_list:
        draft = " [DRAFT]" if pr.get("draft") else ""
        author = pr.get("user", {}).get("login", "?")
        lines.append(
            f"- **#{pr.get('number', '?')}** {pr.get('title', '?')}{draft}\n"
            f"  Author: @{author} | {pr.get('head', {}).get('ref', '?')} -> {pr.get('base', {}).get('ref', '?')}\n"
            f"  Created: {pr.get('created_at', '?')}"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def get_pull_request(owner: str, repo: str, pull_number: int) -> str:
    """
    Get detailed information about a specific pull request.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pull_number: The pull request number.
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            pr = await pulls_ops.aget_pull(client, owner, repo, pull_number)
    except GitHubError as e:
        return _friendly_error(e)

    draft = " (DRAFT)" if pr.get("draft") else ""
    mergeable = pr.get("mergeable")
    merge_status = "Yes" if mergeable is True else ("No" if mergeable is False else "Unknown")

    lines = [
        f"**#{pr.get('number', '?')} {pr.get('title', '?')}**{draft}",
        f"**State:** {pr.get('state', '?')}",
        f"**Author:** @{pr.get('user', {}).get('login', '?')}",
        f"**Branch:** {pr.get('head', {}).get('ref', '?')} -> {pr.get('base', {}).get('ref', '?')}",
        f"**Created:** {pr.get('created_at', '?')}",
        f"**Updated:** {pr.get('updated_at', '?')}",
        f"**Mergeable:** {merge_status}",
        f"**Changed Files:** {pr.get('changed_files', '?')} | **Additions:** +{pr.get('additions', '?')} | **Deletions:** -{pr.get('deletions', '?')}",
        f"**Commits:** {pr.get('commits', '?')}",
        f"**URL:** {pr.get('html_url', '?')}",
    ]

    body = pr.get("body") or "(no description)"
    lines.append(f"\n---\n{body}")

    return "\n".join(lines)


@mcp.tool()
async def create_pull_request(
    owner: str, repo: str, title: str, head: str, base: str,
    body: str = "", draft: str = "false",
) -> str:
    """
    Create a new pull request.

    Args:
        owner: Repository owner.
        repo: Repository name.
        title: Pull request title.
        head: The branch containing changes (e.g., "feature-branch").
        base: The branch to merge into (e.g., "main").
        body: Pull request description (optional).
        draft: Set to "true" to create as draft PR (default: "false").
    """
    token = get_github_token()
    is_draft = draft.lower() == "true"

    try:
        async with AsyncGitHubClient(token) as client:
            pr = await pulls_ops.acreate_pull(
                client, owner, repo, title=title, head=head, base=base,
                body=body, draft=is_draft,
            )
    except GitHubError as e:
        return _friendly_error(e)

    return (
        f"Pull request created: **#{pr.get('number', '?')}** {pr.get('title', '?')}\n"
        f"Branch: {pr.get('head', {}).get('ref', '?')} -> {pr.get('base', {}).get('ref', '?')}\n"
        f"URL: {pr.get('html_url', '?')}"
    )


@mcp.tool()
async def add_pr_comment(owner: str, repo: str, pull_number: int, body: str) -> str:
    """
    Add a general comment to a pull request.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pull_number: The pull request number.
        body: Comment text.
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            comment = await pulls_ops.aadd_pr_comment(client, owner, repo, pull_number, body)
    except GitHubError as e:
        return _friendly_error(e)

    return (
        f"Comment added to PR #{pull_number}.\n"
        f"URL: {comment.get('html_url', '?')}"
    )


@mcp.tool()
async def merge_pull_request(
    owner: str, repo: str, pull_number: int,
    merge_method: str = "merge", commit_title: str = "", commit_message: str = "",
) -> str:
    """
    Merge a pull request.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pull_number: The pull request number to merge.
        merge_method: Merge strategy - merge, squash, or rebase (default: merge).
        commit_title: Custom merge commit title (optional).
        commit_message: Custom merge commit message (optional).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            result = await pulls_ops.amerge_pull(
                client, owner, repo, pull_number,
                merge_method=merge_method,
                commit_title=commit_title, commit_message=commit_message,
            )
    except GitHubError as e:
        return _friendly_error(e)

    return (
        f"Pull request #{pull_number} merged successfully.\n"
        f"SHA: {result.get('sha', '?')}\n"
        f"Message: {result.get('message', '?')}"
    )


# ---------------------------------------------------------------------------
# Code & content tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_file_content(owner: str, repo: str, path: str, ref: str = "") -> str:
    """
    Read a file from a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path within the repository (e.g., "src/main.py").
        ref: Branch, tag, or commit SHA (optional, defaults to default branch).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            data = await code_ops.aget_file_content(client, owner, repo, path, ref=ref)
    except GitHubError as e:
        return _friendly_error(e)

    if isinstance(data, list):
        # Directory listing
        lines = [f"Directory `{path}` contains {len(data)} item(s):\n"]
        for item in data:
            item_type = item.get("type", "?")
            lines.append(f"- [{item_type}] **{item.get('name', '?')}** ({item.get('size', 0)} bytes)")
        return "\n".join(lines)

    name = data.get("name", "?")
    size = data.get("size", 0)
    content = data.get("decoded_content", "")

    if not content:
        return (
            f"**{name}** ({size} bytes)\n"
            f"Binary file — cannot display content.\n"
            f"Download: {data.get('download_url', '?')}"
        )

    ref_str = f" (ref: {ref})" if ref else ""
    return (
        f"**{name}**{ref_str} ({size} bytes)\n\n"
        f"```\n{content}\n```"
    )


@mcp.tool()
async def create_or_update_file(
    owner: str, repo: str, path: str, content: str, message: str,
    sha: str = "", branch: str = "",
) -> str:
    """
    Create or update a file in a GitHub repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path within the repository.
        content: The new file content (plain text).
        message: Commit message for the change.
        sha: SHA of the file being replaced (required for updates, get from get_file_content).
        branch: Target branch (optional, defaults to default branch).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            result = await code_ops.acreate_or_update_file(
                client, owner, repo, path, content=content, message=message,
                sha=sha, branch=branch,
            )
    except GitHubError as e:
        return _friendly_error(e)

    commit = result.get("commit", {})
    action = "Updated" if sha else "Created"
    return (
        f"{action} file `{path}`.\n"
        f"Commit: {commit.get('sha', '?')[:7]} — {commit.get('message', '?')}\n"
        f"URL: {result.get('content', {}).get('html_url', '?')}"
    )


@mcp.tool()
async def search_code(query: str, per_page: int = 10) -> str:
    """
    Search for code across GitHub repositories.

    Args:
        query: Search query (e.g., "addClass repo:jquery/jquery", "filename:.env password").
        per_page: Maximum number of results (default: 10).
    """
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            results = await code_ops.asearch_code(client, query=query, per_page=per_page)
    except GitHubError as e:
        return _friendly_error(e)

    if not results:
        return f'No code found matching "{query}".'

    lines = [f'Found {len(results)} result(s) for "{query}":\n']
    for r in results:
        repo_name = r.get("repository", {}).get("full_name", "?")
        lines.append(
            f"- **{r.get('name', '?')}** in `{repo_name}`\n"
            f"  Path: `{r.get('path', '?')}`\n"
            f"  URL: {r.get('html_url', '?')}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# User tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_authenticated_user() -> str:
    """Get information about the authenticated GitHub user."""
    token = get_github_token()
    try:
        async with AsyncGitHubClient(token) as client:
            user = await code_ops.aget_authenticated_user(client)
    except GitHubError as e:
        return _friendly_error(e)

    lines = [
        f"**{user.get('name') or user.get('login', '?')}** (@{user.get('login', '?')})",
    ]
    if user.get("bio"):
        lines.append(f"**Bio:** {user['bio']}")
    if user.get("company"):
        lines.append(f"**Company:** {user['company']}")
    if user.get("location"):
        lines.append(f"**Location:** {user['location']}")
    lines.extend([
        f"**Public Repos:** {user.get('public_repos', 0)}",
        f"**Followers:** {user.get('followers', 0)} | **Following:** {user.get('following', 0)}",
        f"**URL:** {user.get('html_url', '?')}",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
