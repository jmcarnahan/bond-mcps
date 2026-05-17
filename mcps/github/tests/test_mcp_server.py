"""Tests for the FastMCP server using in-process client.

In-process FastMCP clients don't have HTTP request context, so we mock
get_github_token() directly instead of get_http_headers().
"""

import httpx
import pytest
import respx
from unittest.mock import patch

from github.github_client import GITHUB_API_BASE_URL
from .conftest import (
    SAMPLE_REPO,
    SAMPLE_REPOS_RESPONSE,
    SAMPLE_SEARCH_REPOS_RESPONSE,
    SAMPLE_ISSUE,
    SAMPLE_ISSUES_RESPONSE,
    SAMPLE_COMMENTS_RESPONSE,
    SAMPLE_CREATED_ISSUE,
    SAMPLE_CREATED_COMMENT,
    SAMPLE_PR,
    SAMPLE_PRS_RESPONSE,
    SAMPLE_CREATED_PR,
    SAMPLE_MERGE_RESULT,
    SAMPLE_FILE_CONTENT,
    SAMPLE_DIRECTORY_LISTING,
    SAMPLE_FILE_CREATE_RESULT,
    SAMPLE_CODE_SEARCH_RESPONSE,
    SAMPLE_USER,
    GITHUB_ERROR_401,
    GITHUB_ERROR_404,
    GITHUB_ERROR_422,
)


def _mock_token(token: str = "test-gh-token"):
    """Patch get_github_token to return a test token."""
    return patch("github_mcp.get_github_token", return_value=token)


def _get_text(result) -> str:
    """Extract text from FastMCP CallToolResult."""
    return result.content[0].text


@pytest.fixture
def mcp_server():
    """Import and return the MCP server instance."""
    from github_mcp import mcp
    return mcp


# ---------------------------------------------------------------------------
# Repository tools
# ---------------------------------------------------------------------------

class TestMCPRepoTools:
    """Test repository MCP tools via in-process FastMCP client."""

    @respx.mock
    async def test_list_repositories(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPOS_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_repositories", {})

        text = _get_text(result)
        assert "2 repository(ies)" in text
        assert "octocat/my-project" in text
        assert "octocat/secret-project" in text

    @respx.mock
    async def test_list_repositories_empty(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(200, json=[])
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_repositories", {})

        text = _get_text(result)
        assert "No repositories found" in text

    @respx.mock
    async def test_get_repository(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/octocat/my-project").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPO)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("get_repository", {"owner": "octocat", "repo": "my-project"})

        text = _get_text(result)
        assert "octocat/my-project" in text
        assert "Python" in text
        assert "42" in text  # stars

    @respx.mock
    async def test_search_repositories(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/search/repositories").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_REPOS_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("search_repositories", {"query": "machine learning"})

        text = _get_text(result)
        assert "2 repository(ies)" in text
        assert "octocat/my-project" in text

    @respx.mock
    async def test_search_repositories_empty(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/search/repositories").mock(
            return_value=httpx.Response(200, json={"total_count": 0, "items": []})
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("search_repositories", {"query": "nonexistent"})

        text = _get_text(result)
        assert "No repositories found" in text


# ---------------------------------------------------------------------------
# Issue tools
# ---------------------------------------------------------------------------

class TestMCPIssueTools:
    """Test issue MCP tools via in-process FastMCP client."""

    @respx.mock
    async def test_list_issues(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_issues", {"owner": "o", "repo": "r"})

        text = _get_text(result)
        assert "Fix login bug" in text
        assert "Add dark mode" in text
        # Should filter out PRs
        assert "Fix typo in README" not in text

    @respx.mock
    async def test_list_issues_empty(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(200, json=[])
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_issues", {"owner": "o", "repo": "r"})

        text = _get_text(result)
        assert "No open issues" in text

    @respx.mock
    async def test_get_issue(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUE)
        )
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42/comments").mock(
            return_value=httpx.Response(200, json=SAMPLE_COMMENTS_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("get_issue", {"owner": "o", "repo": "r", "issue_number": 42})

        text = _get_text(result)
        assert "Fix login bug" in text
        assert "bug" in text
        assert "I can reproduce this issue" in text
        assert "Comments (2)" in text

    @respx.mock
    async def test_create_issue(self, mcp_server):
        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "create_issue",
                    {"owner": "o", "repo": "r", "title": "New feature request"},
                )

        text = _get_text(result)
        assert "#50" in text
        assert "created" in text.lower()

    @respx.mock
    async def test_update_issue(self, mcp_server):
        updated = dict(SAMPLE_ISSUE, state="closed")
        respx.patch(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42").mock(
            return_value=httpx.Response(200, json=updated)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "update_issue",
                    {"owner": "o", "repo": "r", "issue_number": 42, "state": "closed"},
                )

        text = _get_text(result)
        assert "updated" in text.lower()
        assert "closed" in text

    @respx.mock
    async def test_add_issue_comment(self, mcp_server):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/42/comments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "add_issue_comment",
                    {"owner": "o", "repo": "r", "issue_number": 42, "body": "Great work!"},
                )

        text = _get_text(result)
        assert "Comment added" in text
        assert "#42" in text


# ---------------------------------------------------------------------------
# Pull request tools
# ---------------------------------------------------------------------------

class TestMCPPullRequestTools:
    """Test pull request MCP tools via in-process FastMCP client."""

    @respx.mock
    async def test_list_pull_requests(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=SAMPLE_PRS_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_pull_requests", {"owner": "o", "repo": "r"})

        text = _get_text(result)
        assert "2 open pull request(s)" in text
        assert "Add login validation" in text
        assert "DRAFT" in text  # Draft PR marker

    @respx.mock
    async def test_list_pull_requests_empty(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=[])
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_pull_requests", {"owner": "o", "repo": "r"})

        text = _get_text(result)
        assert "No open pull requests" in text

    @respx.mock
    async def test_get_pull_request(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/101").mock(
            return_value=httpx.Response(200, json=SAMPLE_PR)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "get_pull_request",
                    {"owner": "o", "repo": "r", "pull_number": 101},
                )

        text = _get_text(result)
        assert "Add login validation" in text
        assert "feature/login-validation" in text
        assert "Mergeable" in text
        assert "+45" in text

    @respx.mock
    async def test_create_pull_request(self, mcp_server):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_PR)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "create_pull_request",
                    {"owner": "o", "repo": "r", "title": "Fix bug", "head": "fix/parser", "base": "main"},
                )

        text = _get_text(result)
        assert "#110" in text
        assert "created" in text.lower()

    @respx.mock
    async def test_add_pr_comment(self, mcp_server):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/101/comments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "add_pr_comment",
                    {"owner": "o", "repo": "r", "pull_number": 101, "body": "LGTM!"},
                )

        text = _get_text(result)
        assert "Comment added" in text
        assert "#101" in text

    @respx.mock
    async def test_merge_pull_request(self, mcp_server):
        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/101/merge").mock(
            return_value=httpx.Response(200, json=SAMPLE_MERGE_RESULT)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "merge_pull_request",
                    {"owner": "o", "repo": "r", "pull_number": 101},
                )

        text = _get_text(result)
        assert "merged successfully" in text
        assert "abc123def456" in text


# ---------------------------------------------------------------------------
# Code & content tools
# ---------------------------------------------------------------------------

class TestMCPCodeTools:
    """Test code and content MCP tools via in-process FastMCP client."""

    @respx.mock
    async def test_get_file_content(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/src/main.py").mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_CONTENT)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "get_file_content",
                    {"owner": "o", "repo": "r", "path": "src/main.py"},
                )

        text = _get_text(result)
        assert "main.py" in text
        assert "import sys" in text

    @respx.mock
    async def test_get_file_content_directory(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/src").mock(
            return_value=httpx.Response(200, json=SAMPLE_DIRECTORY_LISTING)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "get_file_content",
                    {"owner": "o", "repo": "r", "path": "src"},
                )

        text = _get_text(result)
        assert "3 item(s)" in text
        assert "main.py" in text

    @respx.mock
    async def test_create_or_update_file(self, mcp_server):
        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/new.txt").mock(
            return_value=httpx.Response(201, json=SAMPLE_FILE_CREATE_RESULT)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "create_or_update_file",
                    {"owner": "o", "repo": "r", "path": "new.txt", "content": "Hello", "message": "Create"},
                )

        text = _get_text(result)
        assert "Created" in text
        assert "commit4" in text  # SHA is truncated to 7 chars

    @respx.mock
    async def test_search_code(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/search/code").mock(
            return_value=httpx.Response(200, json=SAMPLE_CODE_SEARCH_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("search_code", {"query": "addClass"})

        text = _get_text(result)
        assert "2 result(s)" in text
        assert "main.py" in text
        assert "octocat/my-project" in text

    @respx.mock
    async def test_search_code_empty(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/search/code").mock(
            return_value=httpx.Response(200, json={"total_count": 0, "items": []})
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("search_code", {"query": "nonexistent"})

        text = _get_text(result)
        assert "No code found" in text


# ---------------------------------------------------------------------------
# User tools
# ---------------------------------------------------------------------------

class TestMCPUserTools:
    """Test user MCP tools via in-process FastMCP client."""

    @respx.mock
    async def test_get_authenticated_user(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("get_authenticated_user", {})

        text = _get_text(result)
        assert "The Octocat" in text
        assert "@octocat" in text
        assert "GitHub" in text
        assert "42" in text  # public repos


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestMCPAuth:
    """Test authentication behavior."""

    async def test_missing_token_raises_error(self, mcp_server):
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        with patch("github_mcp.get_github_token", side_effect=PermissionError("Authorization required.")):
            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="Authorization required"):
                    await client.call_tool("list_repositories", {})

    async def test_all_tools_require_auth(self, mcp_server):
        """Verify every tool rejects unauthenticated requests."""
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        tool_calls = [
            ("list_repositories", {}),
            ("get_repository", {"owner": "o", "repo": "r"}),
            ("search_repositories", {"query": "test"}),
            ("list_issues", {"owner": "o", "repo": "r"}),
            ("get_issue", {"owner": "o", "repo": "r", "issue_number": 1}),
            ("create_issue", {"owner": "o", "repo": "r", "title": "T"}),
            ("update_issue", {"owner": "o", "repo": "r", "issue_number": 1}),
            ("add_issue_comment", {"owner": "o", "repo": "r", "issue_number": 1, "body": "X"}),
            ("list_pull_requests", {"owner": "o", "repo": "r"}),
            ("get_pull_request", {"owner": "o", "repo": "r", "pull_number": 1}),
            ("create_pull_request", {"owner": "o", "repo": "r", "title": "T", "head": "h", "base": "b"}),
            ("add_pr_comment", {"owner": "o", "repo": "r", "pull_number": 1, "body": "X"}),
            ("merge_pull_request", {"owner": "o", "repo": "r", "pull_number": 1}),
            ("get_file_content", {"owner": "o", "repo": "r", "path": "f"}),
            ("create_or_update_file", {"owner": "o", "repo": "r", "path": "f", "content": "c", "message": "m"}),
            ("search_code", {"query": "test"}),
            ("get_authenticated_user", {}),
        ]
        with patch("github_mcp.get_github_token", side_effect=PermissionError("Authorization required.")):
            async with Client(mcp_server) as client:
                for tool_name, args in tool_calls:
                    with pytest.raises(ToolError, match="Authorization required"):
                        await client.call_tool(tool_name, args)

    @respx.mock
    async def test_github_401_returns_friendly_message(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(401, json=GITHUB_ERROR_401)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_repositories", {})

        text = _get_text(result)
        assert "authentication failed" in text.lower()
        assert "reconnect" in text.lower()

    @respx.mock
    async def test_github_404_returns_friendly_message(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r").mock(
            return_value=httpx.Response(404, json=GITHUB_ERROR_404)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("get_repository", {"owner": "o", "repo": "r"})

        text = _get_text(result)
        assert "not found" in text.lower()

    @respx.mock
    async def test_github_rate_limit_returns_friendly_message(self, mcp_server):
        respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
            )
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_repositories", {})

        text = _get_text(result)
        assert "rate limit" in text.lower()

    @respx.mock
    async def test_github_422_returns_friendly_message(self, mcp_server):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(422, json=GITHUB_ERROR_422)
        )
        with _mock_token():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "create_issue",
                    {"owner": "o", "repo": "r", "title": "Test"},
                )

        text = _get_text(result)
        assert "validation" in text.lower()
