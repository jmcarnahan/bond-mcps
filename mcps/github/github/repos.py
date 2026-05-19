"""
Repository operations using the GitHub REST API.

All functions accept a GitHubClient or AsyncGitHubClient and return parsed dicts.
"""

from typing import Any

from .github_client import AsyncGitHubClient, GitHubClient

# GitHub API caps per_page at 100
_MAX_PER_PAGE = 100


def _cap(per_page: int) -> int:
    return min(max(per_page, 1), _MAX_PER_PAGE)


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------


def list_repos(
    client: GitHubClient,
    type: str = "all",
    sort: str = "updated",
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List repositories for the authenticated user."""
    data = client.get(
        "/user/repos",
        params={"type": type, "sort": sort, "per_page": _cap(per_page)},
    )
    return data


def get_repo(client: GitHubClient, owner: str, repo: str) -> dict[str, Any]:
    """Get a single repository by owner/repo."""
    return client.get(f"/repos/{owner}/{repo}")


def search_repos(
    client: GitHubClient,
    query: str,
    sort: str = "best-match",
    per_page: int = 10,
) -> list[dict[str, Any]]:
    """Search repositories across GitHub."""
    params: dict[str, Any] = {"q": query, "per_page": _cap(per_page)}
    if sort != "best-match":
        params["sort"] = sort
    data = client.get("/search/repositories", params=params)
    return data.get("items", [])


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------


async def alist_repos(
    client: AsyncGitHubClient,
    type: str = "all",
    sort: str = "updated",
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List repositories for the authenticated user (async)."""
    data = await client.get(
        "/user/repos",
        params={"type": type, "sort": sort, "per_page": _cap(per_page)},
    )
    return data


async def aget_repo(client: AsyncGitHubClient, owner: str, repo: str) -> dict[str, Any]:
    """Get a single repository by owner/repo (async)."""
    return await client.get(f"/repos/{owner}/{repo}")


async def asearch_repos(
    client: AsyncGitHubClient,
    query: str,
    sort: str = "best-match",
    per_page: int = 10,
) -> list[dict[str, Any]]:
    """Search repositories across GitHub (async)."""
    params: dict[str, Any] = {"q": query, "per_page": _cap(per_page)}
    if sort != "best-match":
        params["sort"] = sort
    data = await client.get("/search/repositories", params=params)
    return data.get("items", [])
