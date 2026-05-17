"""Tests for issue operations."""

import httpx
import pytest
import respx

from github.github_client import GITHUB_API_BASE_URL, AsyncGitHubClient, GitHubClient
from .conftest import (
    SAMPLE_ISSUE,
    SAMPLE_ISSUES_RESPONSE,
    SAMPLE_COMMENTS_RESPONSE,
    SAMPLE_CREATED_ISSUE,
    SAMPLE_CREATED_COMMENT,
)


class TestIssuesSync:
    """Synchronous issue operation tests."""

    @respx.mock
    def test_list_issues(self):
        from github.issues import list_issues

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUES_RESPONSE)
        )
        with GitHubClient("tok") as client:
            result = list_issues(client, "o", "r")

        assert len(result) == 3
        assert result[0]["title"] == "Fix login bug"

    @respx.mock
    def test_get_issue(self):
        from github.issues import get_issue

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUE)
        )
        with GitHubClient("tok") as client:
            result = get_issue(client, "o", "r", 42)

        assert result["number"] == 42
        assert result["title"] == "Fix login bug"

    @respx.mock
    def test_get_issue_comments(self):
        from github.issues import get_issue_comments

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42/comments").mock(
            return_value=httpx.Response(200, json=SAMPLE_COMMENTS_RESPONSE)
        )
        with GitHubClient("tok") as client:
            result = get_issue_comments(client, "o", "r", 42)

        assert len(result) == 2
        assert result[0]["body"] == "I can reproduce this issue on Chrome."

    @respx.mock
    def test_create_issue(self):
        from github.issues import create_issue

        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        with GitHubClient("tok") as client:
            result = create_issue(client, "o", "r", title="New feature request", body="Please add this.")

        assert result["number"] == 50
        assert route.called

    @respx.mock
    def test_create_issue_with_labels(self):
        from github.issues import create_issue
        import json

        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        with GitHubClient("tok") as client:
            create_issue(client, "o", "r", title="Bug", labels=["bug", "urgent"])

        body = json.loads(route.calls[0].request.content)
        assert body["labels"] == ["bug", "urgent"]

    @respx.mock
    def test_update_issue(self):
        from github.issues import update_issue

        updated = dict(SAMPLE_ISSUE, state="closed")
        respx.patch(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42").mock(
            return_value=httpx.Response(200, json=updated)
        )
        with GitHubClient("tok") as client:
            result = update_issue(client, "o", "r", 42, state="closed")

        assert result["state"] == "closed"

    @respx.mock
    def test_add_issue_comment(self):
        from github.issues import add_issue_comment

        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42/comments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        with GitHubClient("tok") as client:
            result = add_issue_comment(client, "o", "r", 42, body="This is a new comment.")

        assert result["body"] == "This is a new comment."
        assert route.called


class TestIssuesAsync:
    """Asynchronous issue operation tests."""

    @respx.mock
    async def test_alist_issues(self):
        from github.issues import alist_issues

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUES_RESPONSE)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await alist_issues(client, "o", "r")

        assert len(result) == 3

    @respx.mock
    async def test_aget_issue(self):
        from github.issues import aget_issue

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUE)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await aget_issue(client, "o", "r", 42)

        assert result["number"] == 42

    @respx.mock
    async def test_acreate_issue(self):
        from github.issues import acreate_issue

        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await acreate_issue(client, "o", "r", title="Test")

        assert result["number"] == 50

    @respx.mock
    async def test_aadd_issue_comment(self):
        from github.issues import aadd_issue_comment

        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42/comments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await aadd_issue_comment(client, "o", "r", 42, "New comment")

        assert result["body"] == "This is a new comment."
