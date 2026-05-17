"""Edge case tests — rate limiting, timeouts, large payloads, invalid inputs."""

import httpx
import pytest
import respx

from atlassian.atlassian_client import AsyncAtlassianClient, AtlassianError
from .conftest import CLOUD_ID, JIRA_BASE, CONFLUENCE_V2_BASE

TOKEN = "test-token"


class TestRateLimiting:
    @respx.mock
    async def test_429_with_retry_after(self):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(
                429,
                json={"message": "Rate limit exceeded"},
                headers={"retry-after": "30"},
            )
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            with pytest.raises(AtlassianError) as exc_info:
                await jira.alist_projects(client)
        assert exc_info.value.error_code == "RateLimited"
        assert "30 seconds" in str(exc_info.value)

    @respx.mock
    async def test_429_without_retry_after(self):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(
                429,
                json={"message": "Too many requests"},
            )
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            with pytest.raises(AtlassianError) as exc_info:
                await jira.alist_projects(client)
        assert exc_info.value.error_code == "RateLimited"


class TestEmptyResults:
    @respx.mock
    async def test_search_zero_issues(self):
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await jira.asearch_issues(client, jql="project = EMPTY")
        assert data["issues"] == []

    @respx.mock
    async def test_count_zero(self):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json={"count": 0})
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            count = await jira.acount_issues(client, jql="project = EMPTY")
        assert count == 0

    @respx.mock
    async def test_no_comments(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-1/comment").mock(
            return_value=httpx.Response(200, json={"comments": [], "total": 0})
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            comments = await jira.aget_issue_comments(client, "PROJ-1")
        assert comments == []


class TestInvalidJQL:
    @respx.mock
    async def test_jql_syntax_error(self):
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(
                400,
                json={
                    "errorMessages": [
                        "Error in the JQL Query: Expecting either 'AND' or 'OR' but got 'blah'."
                    ],
                    "errors": {},
                },
            )
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            with pytest.raises(AtlassianError) as exc_info:
                await jira.asearch_issues(client, jql="blah blah blah")
        assert exc_info.value.error_code == "BadRequest"
        assert "JQL" in str(exc_info.value)


class TestLargePayloads:
    @respx.mock
    async def test_large_issue_list(self):
        """Test handling of max page size (100 issues)."""
        big_list = []
        for i in range(100):
            big_list.append({
                "id": str(10000 + i),
                "key": f"PROJ-{i + 1}",
                "fields": {
                    "summary": f"Issue number {i + 1}",
                    "status": {"name": "Open"},
                    "issuetype": {"name": "Task"},
                    "priority": {"name": "Medium"},
                    "assignee": None,
                    "labels": [],
                    "created": "2025-01-01T00:00:00.000+0000",
                    "updated": "2025-01-01T00:00:00.000+0000",
                },
            })
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(
                200,
                json={"issues": big_list, "isLast": False, "nextPageToken": "next"},
            )
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await jira.asearch_issues(client, jql="project = PROJ", max_results=100)
        assert len(data["issues"]) == 100
        assert data["isLast"] is False

    @respx.mock
    async def test_long_description_in_issue(self):
        """Test issue with very long description."""
        long_text = "A" * 10000
        issue = {
            "key": "PROJ-999",
            "fields": {
                "summary": "Issue with long description",
                "status": {"name": "Open"},
                "issuetype": {"name": "Task"},
                "priority": None,
                "assignee": None,
                "reporter": None,
                "labels": [],
                "created": "2025-01-01T00:00:00.000+0000",
                "updated": "2025-01-01T00:00:00.000+0000",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": long_text}],
                        }
                    ],
                },
            },
            "renderedFields": {
                "description": f"<p>{long_text}</p>",
            },
        }
        respx.get(f"{JIRA_BASE}/issue/PROJ-999").mock(
            return_value=httpx.Response(200, json=issue)
        )
        respx.get(f"{JIRA_BASE}/issue/PROJ-999/comment").mock(
            return_value=httpx.Response(200, json={"comments": [], "total": 0})
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await jira.aget_issue(client, "PROJ-999")
        assert result["key"] == "PROJ-999"


class TestCapFunction:
    def test_cap_within_range(self):
        from atlassian.jira import _cap
        assert _cap(50) == 50

    def test_cap_above_max(self):
        from atlassian.jira import _cap
        assert _cap(200) == 100

    def test_cap_below_min(self):
        from atlassian.jira import _cap
        assert _cap(0) == 1

    def test_cap_negative(self):
        from atlassian.jira import _cap
        assert _cap(-5) == 1

    def test_cap_custom_max(self):
        from atlassian.jira import _cap
        assert _cap(50, maximum=25) == 25


class TestTextToAdf:
    def test_simple_text(self):
        from atlassian.jira import _text_to_adf
        result = _text_to_adf("Hello world")
        assert result["type"] == "doc"
        assert result["version"] == 1
        assert result["content"][0]["type"] == "paragraph"
        assert result["content"][0]["content"][0]["text"] == "Hello world"

    def test_multiline_text(self):
        from atlassian.jira import _text_to_adf
        result = _text_to_adf("Line 1\nLine 2\nLine 3")
        assert len(result["content"]) == 3
        assert result["content"][0]["content"][0]["text"] == "Line 1"
        assert result["content"][1]["content"][0]["text"] == "Line 2"
        assert result["content"][2]["content"][0]["text"] == "Line 3"

    def test_empty_string(self):
        from atlassian.jira import _text_to_adf
        result = _text_to_adf("")
        assert result["type"] == "doc"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "paragraph"
        assert result["content"][0]["content"] == []


class TestExtractAdfText:
    def test_simple_paragraph(self):
        from atlassian_mcp import _extract_adf_text
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello "}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "world"}],
                },
            ],
        }
        result = _extract_adf_text(adf)
        assert result == "Hello \nworld"

    def test_empty_doc(self):
        from atlassian_mcp import _extract_adf_text
        adf = {"type": "doc", "version": 1, "content": []}
        assert _extract_adf_text(adf) == ""

    def test_single_paragraph(self):
        from atlassian_mcp import _extract_adf_text
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Just one line"}],
                },
            ],
        }
        assert _extract_adf_text(adf) == "Just one line"

    def test_nested_inline_marks(self):
        """ADF with bold + link inline nodes in one paragraph."""
        from atlassian_mcp import _extract_adf_text
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": " world"},
                    ],
                },
            ],
        }
        assert _extract_adf_text(adf) == "Hello bold world"

    def test_bullet_list(self):
        from atlassian_mcp import _extract_adf_text
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "Item A"}]}
                        ]},
                        {"type": "listItem", "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "Item B"}]}
                        ]},
                    ],
                },
            ],
        }
        result = _extract_adf_text(adf)
        assert "Item A" in result
        assert "Item B" in result
        # Items should be separated
        assert result != "Item AItem B"
