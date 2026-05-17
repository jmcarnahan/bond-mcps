"""Tests for pull request operations."""

import httpx
import pytest
import respx

from github.github_client import GITHUB_API_BASE_URL, AsyncGitHubClient, GitHubClient
from .conftest import (
    SAMPLE_PR,
    SAMPLE_PRS_RESPONSE,
    SAMPLE_CREATED_PR,
    SAMPLE_MERGE_RESULT,
    SAMPLE_CREATED_COMMENT,
)


class TestPullsSync:
    """Synchronous pull request operation tests."""

    @respx.mock
    def test_list_pulls(self):
        from github.pulls import list_pulls

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=SAMPLE_PRS_RESPONSE)
        )
        with GitHubClient("tok") as client:
            result = list_pulls(client, "o", "r")

        assert len(result) == 2
        assert result[0]["title"] == "Add login validation"

    @respx.mock
    def test_get_pull(self):
        from github.pulls import get_pull

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/101").mock(
            return_value=httpx.Response(200, json=SAMPLE_PR)
        )
        with GitHubClient("tok") as client:
            result = get_pull(client, "o", "r", 101)

        assert result["number"] == 101
        assert result["mergeable"] is True

    @respx.mock
    def test_create_pull(self):
        from github.pulls import create_pull
        import json

        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_PR)
        )
        with GitHubClient("tok") as client:
            result = create_pull(client, "o", "r", title="Fix bug", head="fix/parser", base="main")

        assert result["number"] == 110
        body = json.loads(route.calls[0].request.content)
        assert body["head"] == "fix/parser"
        assert body["base"] == "main"

    @respx.mock
    def test_merge_pull(self):
        from github.pulls import merge_pull

        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/101/merge").mock(
            return_value=httpx.Response(200, json=SAMPLE_MERGE_RESULT)
        )
        with GitHubClient("tok") as client:
            result = merge_pull(client, "o", "r", 101, merge_method="squash")

        assert result["merged"] is True

    @respx.mock
    def test_add_pr_comment(self):
        from github.pulls import add_pr_comment

        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/101/comments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        with GitHubClient("tok") as client:
            result = add_pr_comment(client, "o", "r", 101, "LGTM!")

        assert route.called


class TestPullsAsync:
    """Asynchronous pull request operation tests."""

    @respx.mock
    async def test_alist_pulls(self):
        from github.pulls import alist_pulls

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=SAMPLE_PRS_RESPONSE)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await alist_pulls(client, "o", "r")

        assert len(result) == 2

    @respx.mock
    async def test_aget_pull(self):
        from github.pulls import aget_pull

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/101").mock(
            return_value=httpx.Response(200, json=SAMPLE_PR)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await aget_pull(client, "o", "r", 101)

        assert result["number"] == 101

    @respx.mock
    async def test_acreate_pull(self):
        from github.pulls import acreate_pull

        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_PR)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await acreate_pull(client, "o", "r", title="Fix", head="fix", base="main")

        assert result["number"] == 110

    @respx.mock
    async def test_amerge_pull(self):
        from github.pulls import amerge_pull

        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/101/merge").mock(
            return_value=httpx.Response(200, json=SAMPLE_MERGE_RESULT)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await amerge_pull(client, "o", "r", 101)

        assert result["merged"] is True
