"""
Issue operations using the GitHub REST API.

All functions accept a GitHubClient or AsyncGitHubClient and return parsed dicts.
"""

from typing import Any, Dict, List, Optional

from .github_client import GitHubClient, AsyncGitHubClient

# GitHub API caps per_page at 100
_MAX_PER_PAGE = 100


def _cap(per_page: int) -> int:
    return min(max(per_page, 1), _MAX_PER_PAGE)


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------

def list_issues(
    client: GitHubClient,
    owner: str,
    repo: str,
    state: str = "open",
    labels: str = "",
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """List issues in a repository."""
    params: Dict[str, Any] = {"state": state, "per_page": _cap(per_page)}
    if labels:
        params["labels"] = labels
    data = client.get(f"/repos/{owner}/{repo}/issues", params=params)
    return data


def get_issue(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
) -> Dict[str, Any]:
    """Get a single issue by number."""
    return client.get(f"/repos/{owner}/{repo}/issues/{issue_number}")


def get_issue_comments(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """Get comments on an issue."""
    data = client.get(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        params={"per_page": _cap(per_page)},
    )
    return data


def create_issue(
    client: GitHubClient,
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: Optional[List[str]] = None,
    assignees: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new issue."""
    payload: Dict[str, Any] = {"title": title}
    if body:
        payload["body"] = body
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    return client.post(f"/repos/{owner}/{repo}/issues", json_data=payload)


def update_issue(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    title: Optional[str] = None,
    body: Optional[str] = None,
    state: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Update an existing issue."""
    payload: Dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state is not None:
        payload["state"] = state
    if labels is not None:
        payload["labels"] = labels
    return client.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", json_data=payload)


def add_issue_comment(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> Dict[str, Any]:
    """Add a comment to an issue."""
    return client.post(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        json_data={"body": body},
    )


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------

async def alist_issues(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    state: str = "open",
    labels: str = "",
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """List issues in a repository (async)."""
    params: Dict[str, Any] = {"state": state, "per_page": _cap(per_page)}
    if labels:
        params["labels"] = labels
    data = await client.get(f"/repos/{owner}/{repo}/issues", params=params)
    return data


async def aget_issue(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
) -> Dict[str, Any]:
    """Get a single issue by number (async)."""
    return await client.get(f"/repos/{owner}/{repo}/issues/{issue_number}")


async def aget_issue_comments(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """Get comments on an issue (async)."""
    data = await client.get(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        params={"per_page": _cap(per_page)},
    )
    return data


async def acreate_issue(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: Optional[List[str]] = None,
    assignees: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new issue (async)."""
    payload: Dict[str, Any] = {"title": title}
    if body:
        payload["body"] = body
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    return await client.post(f"/repos/{owner}/{repo}/issues", json_data=payload)


async def aupdate_issue(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    title: Optional[str] = None,
    body: Optional[str] = None,
    state: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Update an existing issue (async)."""
    payload: Dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state is not None:
        payload["state"] = state
    if labels is not None:
        payload["labels"] = labels
    return await client.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", json_data=payload)


async def aadd_issue_comment(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> Dict[str, Any]:
    """Add a comment to an issue (async)."""
    return await client.post(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        json_data={"body": body},
    )
