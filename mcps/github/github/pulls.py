"""
Pull request operations using the GitHub REST API.

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

def list_pulls(
    client: GitHubClient,
    owner: str,
    repo: str,
    state: str = "open",
    sort: str = "created",
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """List pull requests in a repository."""
    data = client.get(
        f"/repos/{owner}/{repo}/pulls",
        params={"state": state, "sort": sort, "per_page": _cap(per_page)},
    )
    return data


def get_pull(
    client: GitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
) -> Dict[str, Any]:
    """Get a single pull request by number."""
    return client.get(f"/repos/{owner}/{repo}/pulls/{pull_number}")


def create_pull(
    client: GitHubClient,
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    draft: bool = False,
) -> Dict[str, Any]:
    """Create a new pull request."""
    payload: Dict[str, Any] = {
        "title": title,
        "head": head,
        "base": base,
    }
    if body:
        payload["body"] = body
    if draft:
        payload["draft"] = draft
    return client.post(f"/repos/{owner}/{repo}/pulls", json_data=payload)


def merge_pull(
    client: GitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
    merge_method: str = "merge",
    commit_title: str = "",
    commit_message: str = "",
) -> Dict[str, Any]:
    """Merge a pull request."""
    payload: Dict[str, Any] = {"merge_method": merge_method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message
    return client.put(f"/repos/{owner}/{repo}/pulls/{pull_number}/merge", json_data=payload)


def add_pr_comment(
    client: GitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
    body: str,
) -> Dict[str, Any]:
    """Add a general comment on a pull request (via issues endpoint)."""
    return client.post(
        f"/repos/{owner}/{repo}/issues/{pull_number}/comments",
        json_data={"body": body},
    )


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------

async def alist_pulls(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    state: str = "open",
    sort: str = "created",
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """List pull requests in a repository (async)."""
    data = await client.get(
        f"/repos/{owner}/{repo}/pulls",
        params={"state": state, "sort": sort, "per_page": _cap(per_page)},
    )
    return data


async def aget_pull(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
) -> Dict[str, Any]:
    """Get a single pull request by number (async)."""
    return await client.get(f"/repos/{owner}/{repo}/pulls/{pull_number}")


async def acreate_pull(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    draft: bool = False,
) -> Dict[str, Any]:
    """Create a new pull request (async)."""
    payload: Dict[str, Any] = {
        "title": title,
        "head": head,
        "base": base,
    }
    if body:
        payload["body"] = body
    if draft:
        payload["draft"] = draft
    return await client.post(f"/repos/{owner}/{repo}/pulls", json_data=payload)


async def amerge_pull(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
    merge_method: str = "merge",
    commit_title: str = "",
    commit_message: str = "",
) -> Dict[str, Any]:
    """Merge a pull request (async)."""
    payload: Dict[str, Any] = {"merge_method": merge_method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message
    return await client.put(f"/repos/{owner}/{repo}/pulls/{pull_number}/merge", json_data=payload)


async def aadd_pr_comment(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
    body: str,
) -> Dict[str, Any]:
    """Add a general comment on a pull request (async, via issues endpoint)."""
    return await client.post(
        f"/repos/{owner}/{repo}/issues/{pull_number}/comments",
        json_data={"body": body},
    )
