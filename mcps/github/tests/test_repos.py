"""Tests for repository operations."""

import httpx
import pytest
import respx

from github.github_client import GITHUB_API_BASE_URL, AsyncGitHubClient, GitHubClient
from .conftest import (
    SAMPLE_REPO,
    SAMPLE_REPOS_RESPONSE,
    SAMPLE_SEARCH_REPOS_RESPONSE,
)


class TestReposSync:
    """Synchronous repository operation tests."""

    @respx.mock
    def test_list_repos(self):
        from github.repos import list_repos

        respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPOS_RESPONSE)
        )
        with GitHubClient("tok") as client:
            result = list_repos(client)

        assert len(result) == 2
        assert result[0]["full_name"] == "octocat/my-project"

    @respx.mock
    def test_get_repo(self):
        from github.repos import get_repo

        respx.get(f"{GITHUB_API_BASE_URL}/repos/octocat/my-project").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPO)
        )
        with GitHubClient("tok") as client:
            result = get_repo(client, "octocat", "my-project")

        assert result["full_name"] == "octocat/my-project"
        assert result["language"] == "Python"

    @respx.mock
    def test_search_repos(self):
        from github.repos import search_repos

        respx.get(f"{GITHUB_API_BASE_URL}/search/repositories").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_REPOS_RESPONSE)
        )
        with GitHubClient("tok") as client:
            result = search_repos(client, query="machine learning")

        assert len(result) == 2
        assert result[0]["full_name"] == "octocat/my-project"


class TestReposAsync:
    """Asynchronous repository operation tests."""

    @respx.mock
    async def test_alist_repos(self):
        from github.repos import alist_repos

        respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPOS_RESPONSE)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await alist_repos(client)

        assert len(result) == 2

    @respx.mock
    async def test_aget_repo(self):
        from github.repos import aget_repo

        respx.get(f"{GITHUB_API_BASE_URL}/repos/octocat/my-project").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPO)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await aget_repo(client, "octocat", "my-project")

        assert result["full_name"] == "octocat/my-project"

    @respx.mock
    async def test_asearch_repos(self):
        from github.repos import asearch_repos

        respx.get(f"{GITHUB_API_BASE_URL}/search/repositories").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_REPOS_RESPONSE)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await asearch_repos(client, query="test")

        assert len(result) == 2
