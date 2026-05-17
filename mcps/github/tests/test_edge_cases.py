"""Edge case and regression tests for the GitHub MCP.

Covers:
- per_page capping in domain modules
- Binary file handling in code.py
- GitHubError error_code classification
- Sparse API responses (missing optional fields)
- GitHub API error propagation in domain modules
- MCP tool error messages for various HTTP status codes
"""

import base64
import httpx
import pytest
import respx
from unittest.mock import patch

from github.github_client import (
    GITHUB_API_BASE_URL,
    AsyncGitHubClient,
    GitHubClient,
    GitHubError,
)


# ---------------------------------------------------------------------------
# per_page capping
# ---------------------------------------------------------------------------

class TestPerPageCapping:
    """Verify all domain modules cap per_page to 100."""

    @respx.mock
    def test_repos_caps_per_page_to_100(self):
        from github.repos import list_repos

        route = respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(200, json=[])
        )
        with GitHubClient("tok") as client:
            list_repos(client, per_page=500)

        assert "per_page=100" in str(route.calls[0].request.url)

    @respx.mock
    def test_repos_caps_per_page_minimum_to_1(self):
        from github.repos import list_repos

        route = respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(200, json=[])
        )
        with GitHubClient("tok") as client:
            list_repos(client, per_page=0)

        assert "per_page=1" in str(route.calls[0].request.url)

    @respx.mock
    def test_issues_caps_per_page_to_100(self):
        from github.issues import list_issues

        route = respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(200, json=[])
        )
        with GitHubClient("tok") as client:
            list_issues(client, "o", "r", per_page=200)

        assert "per_page=100" in str(route.calls[0].request.url)

    @respx.mock
    def test_issue_comments_caps_per_page(self):
        from github.issues import get_issue_comments

        route = respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/1/comments").mock(
            return_value=httpx.Response(200, json=[])
        )
        with GitHubClient("tok") as client:
            get_issue_comments(client, "o", "r", 1, per_page=999)

        assert "per_page=100" in str(route.calls[0].request.url)

    @respx.mock
    def test_pulls_caps_per_page_to_100(self):
        from github.pulls import list_pulls

        route = respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(200, json=[])
        )
        with GitHubClient("tok") as client:
            list_pulls(client, "o", "r", per_page=300)

        assert "per_page=100" in str(route.calls[0].request.url)

    @respx.mock
    def test_search_repos_caps_per_page(self):
        from github.repos import search_repos

        route = respx.get(f"{GITHUB_API_BASE_URL}/search/repositories").mock(
            return_value=httpx.Response(200, json={"total_count": 0, "items": []})
        )
        with GitHubClient("tok") as client:
            search_repos(client, query="test", per_page=200)

        assert "per_page=100" in str(route.calls[0].request.url)

    @respx.mock
    def test_search_code_caps_per_page(self):
        from github.code import search_code

        route = respx.get(f"{GITHUB_API_BASE_URL}/search/code").mock(
            return_value=httpx.Response(200, json={"total_count": 0, "items": []})
        )
        with GitHubClient("tok") as client:
            search_code(client, query="test", per_page=150)

        assert "per_page=100" in str(route.calls[0].request.url)


# ---------------------------------------------------------------------------
# Binary file handling in code.py
# ---------------------------------------------------------------------------

class TestBinaryFileHandling:
    """Test that binary files are handled safely without corruption."""

    @respx.mock
    def test_binary_file_returns_none_decoded_content(self):
        from github.code import get_file_content

        # Create binary content that can't be decoded as UTF-8
        binary_data = bytes(range(128, 256))
        b64 = base64.b64encode(binary_data).decode("ascii")

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/image.png").mock(
            return_value=httpx.Response(200, json={
                "name": "image.png",
                "path": "image.png",
                "sha": "abc",
                "size": len(binary_data),
                "encoding": "base64",
                "content": b64,
            })
        )
        with GitHubClient("tok") as client:
            result = get_file_content(client, "o", "r", "image.png")

        assert result["decoded_content"] is None
        assert "Binary file" in result.get("_decode_note", "")

    @respx.mock
    def test_text_file_decodes_correctly(self):
        from github.code import get_file_content

        text = "Hello, World!\nLine 2\n"
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/hello.txt").mock(
            return_value=httpx.Response(200, json={
                "name": "hello.txt",
                "path": "hello.txt",
                "sha": "abc",
                "size": len(text),
                "encoding": "base64",
                "content": b64,
            })
        )
        with GitHubClient("tok") as client:
            result = get_file_content(client, "o", "r", "hello.txt")

        assert result["decoded_content"] == text

    @respx.mock
    def test_large_file_skips_decode(self):
        from github.code import get_file_content, MAX_TEXT_DECODE_BYTES

        b64 = base64.b64encode(b"x").decode("ascii")

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/huge.bin").mock(
            return_value=httpx.Response(200, json={
                "name": "huge.bin",
                "path": "huge.bin",
                "sha": "abc",
                "size": MAX_TEXT_DECODE_BYTES + 1,
                "encoding": "base64",
                "content": b64,
            })
        )
        with GitHubClient("tok") as client:
            result = get_file_content(client, "o", "r", "huge.bin")

        assert result["decoded_content"] is None
        assert "too large" in result.get("_decode_note", "")

    @respx.mock
    def test_file_without_base64_encoding_passes_through(self):
        from github.code import get_file_content

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/submodule").mock(
            return_value=httpx.Response(200, json={
                "name": "submodule",
                "path": "submodule",
                "sha": "abc",
                "size": 0,
                "type": "submodule",
            })
        )
        with GitHubClient("tok") as client:
            result = get_file_content(client, "o", "r", "submodule")

        # No encoding field means _decode_file_content should pass through
        assert "decoded_content" not in result

    @respx.mock
    async def test_async_binary_file_returns_none(self):
        from github.code import aget_file_content

        binary_data = bytes(range(128, 256))
        b64 = base64.b64encode(binary_data).decode("ascii")

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/icon.ico").mock(
            return_value=httpx.Response(200, json={
                "name": "icon.ico",
                "path": "icon.ico",
                "sha": "abc",
                "size": len(binary_data),
                "encoding": "base64",
                "content": b64,
            })
        )
        async with AsyncGitHubClient("tok") as client:
            result = await aget_file_content(client, "o", "r", "icon.ico")

        assert result["decoded_content"] is None


# ---------------------------------------------------------------------------
# GitHubError classification
# ---------------------------------------------------------------------------

class TestGitHubErrorClassification:
    """Test that GitHubError includes proper error_code classification."""

    @respx.mock
    def test_401_classified_as_bad_credentials(self):
        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.get("/user")

        assert exc_info.value.error_code == "BadCredentials"

    @respx.mock
    def test_404_classified_as_not_found(self):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.get("/repos/o/r")

        assert exc_info.value.error_code == "NotFound"

    @respx.mock
    def test_422_classified_as_validation_failed(self):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(422, json={
                "message": "Validation Failed",
                "errors": [{"message": "title is required"}],
            })
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.post("/repos/o/r/issues", json_data={})

        assert exc_info.value.error_code == "ValidationFailed"

    @respx.mock
    def test_403_rate_limit_classified(self):
        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
            )
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.get("/user")

        assert exc_info.value.error_code == "RateLimitExceeded"
        assert "9999999999" in str(exc_info.value)

    @respx.mock
    def test_error_message_capped(self):
        # A very long error message should be truncated
        long_msg = "x" * 5000
        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(500, text=long_msg)
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.get("/user")

        # The message inside the exception should not contain the full 5000 chars
        assert len(str(exc_info.value)) < 2000


# ---------------------------------------------------------------------------
# Sparse API responses
# ---------------------------------------------------------------------------

class TestSparseResponses:
    """Test that tools handle responses with missing optional fields."""

    @respx.mock
    async def test_repo_with_no_description_or_language(self):
        from .conftest import SAMPLE_REPO
        sparse_repo = {
            "full_name": "user/minimal",
            "private": False,
        }
        respx.get(f"{GITHUB_API_BASE_URL}/repos/user/minimal").mock(
            return_value=httpx.Response(200, json=sparse_repo)
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool("get_repository", {"owner": "user", "repo": "minimal"})

        text = result.content[0].text
        assert "user/minimal" in text
        # Should not crash even with missing fields

    @respx.mock
    async def test_issue_with_no_labels_or_assignees(self):
        sparse_issue = {
            "number": 1,
            "title": "Minimal issue",
            "state": "open",
            "user": {"login": "someone"},
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
            "labels": [],
            "assignees": [],
            "html_url": "https://github.com/o/r/issues/1",
        }
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/1").mock(
            return_value=httpx.Response(200, json=sparse_issue)
        )
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/1/comments").mock(
            return_value=httpx.Response(200, json=[])
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool("get_issue", {"owner": "o", "repo": "r", "issue_number": 1})

        text = result.content[0].text
        assert "Minimal issue" in text
        # Should not include Labels or Assignees lines

    @respx.mock
    async def test_pr_with_null_mergeable(self):
        sparse_pr = {
            "number": 1,
            "title": "WIP",
            "state": "open",
            "draft": True,
            "user": {"login": "dev"},
            "head": {"ref": "feat", "sha": "aaa"},
            "base": {"ref": "main", "sha": "bbb"},
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
            "mergeable": None,
            "changed_files": 0,
            "additions": 0,
            "deletions": 0,
            "commits": 0,
            "html_url": "https://github.com/o/r/pull/1",
        }
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/1").mock(
            return_value=httpx.Response(200, json=sparse_pr)
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool("get_pull_request", {"owner": "o", "repo": "r", "pull_number": 1})

        text = result.content[0].text
        assert "Unknown" in text  # mergeable = None -> "Unknown"

    @respx.mock
    async def test_user_with_no_optional_fields(self):
        sparse_user = {
            "login": "minuser",
            "id": 999,
            "public_repos": 0,
            "followers": 0,
            "following": 0,
            "html_url": "https://github.com/minuser",
        }
        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(200, json=sparse_user)
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool("get_authenticated_user", {})

        text = result.content[0].text
        assert "@minuser" in text
        # Should not include Bio, Company, Location

    @respx.mock
    async def test_binary_file_shows_download_url(self):
        """When decoded_content is None, the MCP tool should show download URL."""
        binary_data = bytes(range(128, 256))
        b64 = base64.b64encode(binary_data).decode("ascii")

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/photo.jpg").mock(
            return_value=httpx.Response(200, json={
                "name": "photo.jpg",
                "path": "photo.jpg",
                "sha": "abc",
                "size": len(binary_data),
                "encoding": "base64",
                "content": b64,
                "download_url": "https://raw.githubusercontent.com/o/r/main/photo.jpg",
            })
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "get_file_content",
                    {"owner": "o", "repo": "r", "path": "photo.jpg"},
                )

        text = result.content[0].text
        assert "Binary file" in text
        assert "Download:" in text


# ---------------------------------------------------------------------------
# Domain module error propagation
# ---------------------------------------------------------------------------

class TestDomainModuleErrors:
    """Test that domain modules properly propagate GitHubError."""

    @respx.mock
    def test_get_repo_404(self):
        from github.repos import get_repo

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/nonexistent").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                get_repo(client, "o", "nonexistent")

        assert exc_info.value.status_code == 404
        assert exc_info.value.error_code == "NotFound"

    @respx.mock
    def test_get_issue_404(self):
        from github.issues import get_issue

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/99999").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                get_issue(client, "o", "r", 99999)

        assert exc_info.value.status_code == 404

    @respx.mock
    def test_get_pull_404(self):
        from github.pulls import get_pull

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/99999").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                get_pull(client, "o", "r", 99999)

        assert exc_info.value.status_code == 404

    @respx.mock
    def test_create_issue_422(self):
        from github.issues import create_issue

        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(422, json={
                "message": "Validation Failed",
                "errors": [{"message": "title is missing"}],
            })
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                create_issue(client, "o", "r", title="")

        assert exc_info.value.status_code == 422
        assert "title is missing" in str(exc_info.value)

    @respx.mock
    def test_merge_pull_409_conflict(self):
        from github.pulls import merge_pull

        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/1/merge").mock(
            return_value=httpx.Response(409, json={
                "message": "Pull Request is not mergeable",
            })
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                merge_pull(client, "o", "r", 1)

        assert exc_info.value.status_code == 409

    @respx.mock
    async def test_async_get_file_content_401(self):
        from github.code import aget_file_content

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/secret.txt").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        async with AsyncGitHubClient("bad-tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                await aget_file_content(client, "o", "r", "secret.txt")

        assert exc_info.value.error_code == "BadCredentials"


# ---------------------------------------------------------------------------
# MCP tool-level error messages across multiple tools
# ---------------------------------------------------------------------------

class TestMCPToolErrors:
    """Test that various MCP tools return user-friendly error messages."""

    @respx.mock
    async def test_get_issue_404_friendly(self):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/99999").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "get_issue",
                    {"owner": "o", "repo": "r", "issue_number": 99999},
                )

        text = result.content[0].text
        assert "not found" in text.lower()

    @respx.mock
    async def test_merge_pr_409_friendly(self):
        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/1/merge").mock(
            return_value=httpx.Response(409, json={"message": "Not mergeable"})
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "merge_pull_request",
                    {"owner": "o", "repo": "r", "pull_number": 1},
                )

        text = result.content[0].text
        assert "error" in text.lower() or "not mergeable" in text.lower()

    @respx.mock
    async def test_search_code_401_friendly(self):
        respx.get(f"{GITHUB_API_BASE_URL}/search/code").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool("search_code", {"query": "test"})

        text = result.content[0].text
        assert "authentication failed" in text.lower()

    @respx.mock
    async def test_create_pr_422_friendly(self):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls").mock(
            return_value=httpx.Response(422, json={
                "message": "Validation Failed",
                "errors": [{"message": "No commits between main and main"}],
            })
        )
        with patch("github_mcp.get_github_token", return_value="tok"):
            from github_mcp import mcp
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "create_pull_request",
                    {"owner": "o", "repo": "r", "title": "T", "head": "main", "base": "main"},
                )

        text = result.content[0].text
        assert "validation" in text.lower()
