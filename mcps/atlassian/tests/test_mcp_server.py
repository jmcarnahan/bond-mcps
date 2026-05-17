"""Tests for the FastMCP server using in-process client.

In-process FastMCP clients don't have HTTP request context, so we mock
get_atlassian_token() and get_cloud_id() directly.

Tests the consolidated 5-tool structure:
  Jira       : jira_search, jira_get, jira_manage
  Confluence : confluence_search, confluence_manage
"""

import httpx
import pytest
import respx
from unittest.mock import patch

from .conftest import (
    CLOUD_ID,
    JIRA_BASE,
    CONFLUENCE_V1_BASE,
    CONFLUENCE_V2_BASE,
    SAMPLE_PROJECTS_RESPONSE,
    SAMPLE_SEARCH_RESPONSE,
    SAMPLE_SEARCH_RESPONSE_PAGINATED,
    SAMPLE_COUNT_RESPONSE,
    SAMPLE_ISSUE,
    SAMPLE_COMMENTS_RESPONSE,
    SAMPLE_CREATED_ISSUE,
    SAMPLE_CREATED_COMMENT,
    SAMPLE_TRANSITIONS,
    SAMPLE_SPACES_RESPONSE,
    SAMPLE_CONFLUENCE_SEARCH_RESPONSE,
    SAMPLE_PAGE,
    SAMPLE_CREATED_PAGE,
    SAMPLE_UPDATED_PAGE,
    SAMPLE_USER,
    ATLASSIAN_ERROR_401,
    ATLASSIAN_ERROR_404,
    ATLASSIAN_ERROR_400_JIRA,
    ATLASSIAN_ERROR_429,
    SAMPLE_VERSIONS_RESPONSE,
    SAMPLE_CREATED_VERSION,
    SAMPLE_USERS_RESPONSE,
)


def _mock_auth(token: str = "test-token", cloud_id: str = CLOUD_ID):
    """Context manager to mock both auth functions."""
    return _MultiPatch(token, cloud_id)


class _MultiPatch:
    """Helper to patch both get_atlassian_token and get_cloud_id."""

    def __init__(self, token, cloud_id):
        self._token_patch = patch("atlassian_mcp.get_atlassian_token", return_value=token)
        self._cloud_patch = patch("atlassian_mcp.get_cloud_id", return_value=cloud_id)

    def __enter__(self):
        self._token_patch.start()
        self._cloud_patch.start()
        return self

    def __exit__(self, *args):
        self._token_patch.stop()
        self._cloud_patch.stop()


def _get_text(result) -> str:
    """Extract text from FastMCP CallToolResult."""
    return result.content[0].text


@pytest.fixture
def mcp_server():
    """Import and return the MCP server instance."""
    from atlassian_mcp import mcp
    return mcp


# ---------------------------------------------------------------------------
# jira_search
# ---------------------------------------------------------------------------

class TestJiraSearch:
    @respx.mock
    async def test_search_projects(self, mcp_server):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(200, json=SAMPLE_PROJECTS_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "projects"})

        text = _get_text(result)
        assert "2 project(s)" in text
        assert "PROJ" in text
        assert "HR" in text

    @respx.mock
    async def test_search_projects_empty(self, mcp_server):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(200, json={"values": [], "total": 0})
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "projects"})

        text = _get_text(result)
        assert "No Jira projects found" in text

    @respx.mock
    async def test_search_issues(self, mcp_server):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json={"count": 2})
        )
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "issues", "jql": "project = PROJ"}
                )

        text = _get_text(result)
        assert "2" in text
        assert "PROJ-42" in text
        assert "Fix login timeout bug" in text
        assert "In Progress" in text

    @respx.mock
    async def test_search_issues_empty(self, mcp_server):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json={"count": 0})
        )
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "issues", "jql": "project = NONE"}
                )

        text = _get_text(result)
        assert "No issues found" in text

    @respx.mock
    async def test_search_issues_paginated(self, mcp_server):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json={"count": 500})
        )
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE_PAGINATED),
                httpx.Response(200, json={"issues": [], "isLast": True}),
            ]
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "issues", "jql": "project = PROJ"}
                )

        text = _get_text(result)
        assert "1" in text
        assert "500" in text

    @respx.mock
    async def test_search_issues_extended_fields(self, mcp_server):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json={"count": 2})
        )
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search",
                    {"target": "issues", "jql": "project = PROJ", "fields": "extended"},
                )

        text = _get_text(result)
        assert "PROJ-42" in text
        assert "Assignee:" in text or "Alice Smith" in text

    @respx.mock
    async def test_search_issues_missing_jql(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "issues"})

        text = _get_text(result)
        assert "jql" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_search_issue_count(self, mcp_server):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json=SAMPLE_COUNT_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "issue_count", "jql": "project = PROJ"}
                )

        text = _get_text(result)
        assert "42" in text

    @respx.mock
    async def test_search_issue_count_missing_jql(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "issue_count"})

        text = _get_text(result)
        assert "jql" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_search_versions(self, mcp_server):
        respx.get(f"{JIRA_BASE}/project/PROJ/version").mock(
            return_value=httpx.Response(200, json=SAMPLE_VERSIONS_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "versions", "project_key": "PROJ"}
                )

        text = _get_text(result)
        assert "2 version(s)" in text
        assert "1.0.0" in text
        assert "1.1.0" in text
        assert "released" in text
        assert "unreleased" in text

    @respx.mock
    async def test_search_versions_empty(self, mcp_server):
        respx.get(f"{JIRA_BASE}/project/PROJ/version").mock(
            return_value=httpx.Response(200, json={"values": [], "isLast": True})
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "versions", "project_key": "PROJ"}
                )

        text = _get_text(result)
        assert "No versions found" in text

    @respx.mock
    async def test_search_versions_with_status_filter(self, mcp_server):
        respx.get(f"{JIRA_BASE}/project/PROJ/version").mock(
            return_value=httpx.Response(200, json={
                "values": [SAMPLE_VERSIONS_RESPONSE["values"][0]],
                "isLast": True,
            })
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search",
                    {"target": "versions", "project_key": "PROJ", "status": "released"},
                )

        text = _get_text(result)
        assert "1.0.0" in text

    @respx.mock
    async def test_search_versions_missing_project_key(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "versions"})

        text = _get_text(result)
        assert "project_key" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_search_users(self, mcp_server):
        respx.get(f"{JIRA_BASE}/user/search").mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "users", "query": "alice"}
                )

        text = _get_text(result)
        assert "Alice Smith" in text
        assert "5b10ac8d82e05b22cc7d4ef5" in text
        assert "alice@example.com" in text

    @respx.mock
    async def test_search_users_empty(self, mcp_server):
        respx.get(f"{JIRA_BASE}/user/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_search", {"target": "users", "query": "nobody"}
                )

        text = _get_text(result)
        assert "No users found" in text

    @respx.mock
    async def test_search_users_missing_query(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "users"})

        text = _get_text(result)
        assert "query" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_search_myself(self, mcp_server):
        respx.get(f"{JIRA_BASE}/myself").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "myself"})

        text = _get_text(result)
        assert "Alice Smith" in text
        assert "alice@example.com" in text

    async def test_search_invalid_target(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "invalid"})

        text = _get_text(result)
        assert "Invalid target" in text
        assert "projects" in text


# ---------------------------------------------------------------------------
# jira_get
# ---------------------------------------------------------------------------

class TestJiraGet:
    @respx.mock
    async def test_get_issue(self, mcp_server):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUE)
        )
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/comment").mock(
            return_value=httpx.Response(200, json=SAMPLE_COMMENTS_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_get", {"target": "issue", "issue_key": "PROJ-42"}
                )

        text = _get_text(result)
        assert "PROJ-42" in text
        assert "Fix login timeout bug" in text
        assert "In Progress" in text
        assert "Alice Smith" in text
        assert "Comments (2)" in text
        assert "I can reproduce" in text

    @respx.mock
    async def test_get_issue_comments_fail_gracefully(self, mcp_server):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUE)
        )
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/comment").mock(
            return_value=httpx.Response(403, json={
                "errorMessages": ["Forbidden"], "errors": {}
            })
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_get", {"target": "issue", "issue_key": "PROJ-42"}
                )

        text = _get_text(result)
        assert "PROJ-42" in text
        assert "Fix login timeout bug" in text
        assert "Comments" not in text

    @respx.mock
    async def test_get_transitions(self, mcp_server):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json=SAMPLE_TRANSITIONS)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_get", {"target": "transitions", "issue_key": "PROJ-42"}
                )

        text = _get_text(result)
        assert "PROJ-42" in text
        assert "To Do" in text
        assert "In Progress" in text
        assert "Done" in text

    @respx.mock
    async def test_get_transitions_empty(self, mcp_server):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json={"transitions": []})
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_get", {"target": "transitions", "issue_key": "PROJ-42"}
                )

        text = _get_text(result)
        assert "No transitions" in text

    async def test_get_missing_issue_key(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_get", {"target": "issue"})

        text = _get_text(result)
        assert "issue_key" in text.lower() and "required" in text.lower()

    async def test_get_invalid_target(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_get", {"target": "invalid"})

        text = _get_text(result)
        assert "Invalid target" in text
        assert "issue" in text


# ---------------------------------------------------------------------------
# jira_manage
# ---------------------------------------------------------------------------

class TestJiraManage:
    @respx.mock
    async def test_create_issue(self, mcp_server):
        respx.post(f"{JIRA_BASE}/issue").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "create_issue", "project_key": "PROJ", "summary": "New bug report"},
                )

        text = _get_text(result)
        assert "PROJ-100" in text
        assert "created" in text.lower()

    async def test_create_issue_missing_project_key(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "create_issue", "summary": "Bug"},
                )

        text = _get_text(result)
        assert "project_key" in text.lower() and "required" in text.lower()

    async def test_create_issue_missing_summary(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "create_issue", "project_key": "PROJ"},
                )

        text = _get_text(result)
        assert "summary" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_update_issue(self, mcp_server):
        respx.put(f"{JIRA_BASE}/issue/PROJ-42").mock(
            return_value=httpx.Response(204)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "update_issue", "issue_key": "PROJ-42", "summary": "Updated title"},
                )

        text = _get_text(result)
        assert "PROJ-42" in text
        assert "updated" in text.lower()

    async def test_update_issue_missing_issue_key(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "update_issue", "summary": "New title"},
                )

        text = _get_text(result)
        assert "issue_key" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_comment(self, mcp_server):
        respx.post(f"{JIRA_BASE}/issue/PROJ-42/comment").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "comment", "issue_key": "PROJ-42", "body": "Great work!"},
                )

        text = _get_text(result)
        assert "Comment added" in text
        assert "PROJ-42" in text

    @respx.mock
    async def test_comment_with_author_label(self, mcp_server):
        route = respx.post(f"{JIRA_BASE}/issue/PROJ-42/comment").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {
                        "target": "comment",
                        "issue_key": "PROJ-42",
                        "body": "Looks good.",
                        "author_label": "AI Agent",
                    },
                )

        text = _get_text(result)
        assert "Comment added" in text
        body = route.calls[0].request.content
        assert b"AI Agent" in body
        assert b"Bond AI" in body

    @respx.mock
    async def test_comment_with_mention(self, mcp_server):
        route = respx.post(f"{JIRA_BASE}/issue/PROJ-42/comment").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {
                        "target": "comment",
                        "issue_key": "PROJ-42",
                        "body": "Hi @{5b10ac8d82e05b22cc7d4ef5} please review",
                    },
                )

        text = _get_text(result)
        assert "Comment added" in text
        body = route.calls[0].request.content
        assert b"mention" in body
        assert b"5b10ac8d82e05b22cc7d4ef5" in body

    async def test_comment_missing_issue_key(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "comment", "body": "text"},
                )

        text = _get_text(result)
        assert "issue_key" in text.lower() and "required" in text.lower()

    async def test_comment_missing_body(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "comment", "issue_key": "PROJ-42"},
                )

        text = _get_text(result)
        assert "body" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_transition(self, mcp_server):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json=SAMPLE_TRANSITIONS)
        )
        respx.post(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(204)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "transition", "issue_key": "PROJ-42", "transition_name": "Done"},
                )

        text = _get_text(result)
        assert "PROJ-42" in text
        assert "Done" in text
        assert "transitioned" in text.lower()

    @respx.mock
    async def test_transition_invalid(self, mcp_server):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json=SAMPLE_TRANSITIONS)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "transition", "issue_key": "PROJ-42", "transition_name": "Invalid"},
                )

        text = _get_text(result)
        assert "not available" in text.lower() or "rejected" in text.lower()

    async def test_transition_missing_issue_key(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "transition", "transition_name": "Done"},
                )

        text = _get_text(result)
        assert "issue_key" in text.lower() and "required" in text.lower()

    async def test_transition_missing_name(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "transition", "issue_key": "PROJ-42"},
                )

        text = _get_text(result)
        assert "transition_name" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_create_version(self, mcp_server):
        route = respx.post(f"{JIRA_BASE}/version").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_VERSION)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {
                        "target": "create_version",
                        "project_key": "PROJ",
                        "name": "2.0.0",
                        "release_date": "2026-12-01",
                    },
                )

        text = _get_text(result)
        assert "2.0.0" in text
        assert "created" in text.lower()
        assert "10302" in text
        body = route.calls[0].request.content
        assert b"PROJ" in body
        assert b"2.0.0" in body

    async def test_create_version_missing_project_key(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "create_version", "name": "1.0.0"},
                )

        text = _get_text(result)
        assert "project_key" in text.lower() and "required" in text.lower()

    async def test_create_version_missing_name(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "create_version", "project_key": "PROJ"},
                )

        text = _get_text(result)
        assert "name" in text.lower() and "required" in text.lower()

    async def test_manage_invalid_target(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_manage", {"target": "invalid"})

        text = _get_text(result)
        assert "Invalid target" in text
        assert "create_issue" in text


# ---------------------------------------------------------------------------
# confluence_search
# ---------------------------------------------------------------------------

class TestConfluenceSearch:
    @respx.mock
    async def test_search_spaces(self, mcp_server):
        respx.get(f"{CONFLUENCE_V2_BASE}/spaces").mock(
            return_value=httpx.Response(200, json=SAMPLE_SPACES_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("confluence_search", {"target": "spaces"})

        text = _get_text(result)
        assert "2 space(s)" in text
        assert "DEV" in text
        assert "HR" in text

    @respx.mock
    async def test_search_spaces_empty(self, mcp_server):
        respx.get(f"{CONFLUENCE_V2_BASE}/spaces").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("confluence_search", {"target": "spaces"})

        text = _get_text(result)
        assert "No Confluence spaces found" in text

    @respx.mock
    async def test_search_pages(self, mcp_server):
        respx.get(f"{CONFLUENCE_V1_BASE}/content/search").mock(
            return_value=httpx.Response(200, json=SAMPLE_CONFLUENCE_SEARCH_RESPONSE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_search", {"target": "pages", "query": "type = page"}
                )

        text = _get_text(result)
        assert "2 result(s)" in text
        assert "Architecture Overview" in text
        assert "Q4 Release Notes" in text

    @respx.mock
    async def test_search_pages_empty(self, mcp_server):
        respx.get(f"{CONFLUENCE_V1_BASE}/content/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_search", {"target": "pages", "query": "nonexistent"}
                )

        text = _get_text(result)
        assert "No content found" in text

    async def test_search_pages_missing_query(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("confluence_search", {"target": "pages"})

        text = _get_text(result)
        assert "query" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_get_page(self, mcp_server):
        respx.get(f"{CONFLUENCE_V2_BASE}/pages/12345").mock(
            return_value=httpx.Response(200, json=SAMPLE_PAGE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_search", {"target": "page", "page_id": "12345"}
                )

        text = _get_text(result)
        assert "Architecture Overview" in text
        assert "microservices" in text

    async def test_get_page_missing_page_id(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("confluence_search", {"target": "page"})

        text = _get_text(result)
        assert "page_id" in text.lower() and "required" in text.lower()

    async def test_search_invalid_target(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("confluence_search", {"target": "invalid"})

        text = _get_text(result)
        assert "Invalid target" in text
        assert "spaces" in text


# ---------------------------------------------------------------------------
# confluence_manage
# ---------------------------------------------------------------------------

class TestConfluenceManage:
    @respx.mock
    async def test_create_page(self, mcp_server):
        respx.post(f"{CONFLUENCE_V2_BASE}/pages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CREATED_PAGE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_manage",
                    {
                        "target": "create_page",
                        "space_id": "65536",
                        "title": "New Page",
                        "body": "<p>Hello</p>",
                    },
                )

        text = _get_text(result)
        assert "New Page" in text
        assert "12400" in text

    async def test_create_page_missing_space_id(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_manage",
                    {"target": "create_page", "title": "X", "body": "<p>Y</p>"},
                )

        text = _get_text(result)
        assert "space_id" in text.lower() and "required" in text.lower()

    async def test_create_page_missing_title(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_manage",
                    {"target": "create_page", "space_id": "123", "body": "<p>Y</p>"},
                )

        text = _get_text(result)
        assert "title" in text.lower() and "required" in text.lower()

    async def test_create_page_missing_body(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_manage",
                    {"target": "create_page", "space_id": "123", "title": "X"},
                )

        text = _get_text(result)
        assert "body" in text.lower() and "required" in text.lower()

    @respx.mock
    async def test_update_page_explicit_version(self, mcp_server):
        respx.put(f"{CONFLUENCE_V2_BASE}/pages/12345").mock(
            return_value=httpx.Response(200, json=SAMPLE_UPDATED_PAGE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_manage",
                    {
                        "target": "update_page",
                        "page_id": "12345",
                        "title": "Architecture Overview v2",
                        "body": "<h1>Updated</h1>",
                        "version_number": 4,
                    },
                )

        text = _get_text(result)
        assert "Architecture Overview v2" in text
        assert "version 4" in text

    @respx.mock
    async def test_update_page_auto_version(self, mcp_server):
        respx.get(f"{CONFLUENCE_V2_BASE}/pages/12345").mock(
            return_value=httpx.Response(200, json=SAMPLE_PAGE)
        )
        respx.put(f"{CONFLUENCE_V2_BASE}/pages/12345").mock(
            return_value=httpx.Response(200, json=SAMPLE_UPDATED_PAGE)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_manage",
                    {
                        "target": "update_page",
                        "page_id": "12345",
                        "title": "Architecture Overview v2",
                        "body": "<h1>Updated</h1>",
                    },
                )

        text = _get_text(result)
        assert "Architecture Overview v2" in text
        assert "version 4" in text

    async def test_update_page_missing_page_id(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "confluence_manage",
                    {"target": "update_page", "title": "X", "body": "<p>Y</p>"},
                )

        text = _get_text(result)
        assert "page_id" in text.lower() and "required" in text.lower()

    async def test_manage_invalid_target(self, mcp_server):
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("confluence_manage", {"target": "invalid"})

        text = _get_text(result)
        assert "Invalid target" in text
        assert "create_page" in text


# ---------------------------------------------------------------------------
# Auth error handling
# ---------------------------------------------------------------------------

class TestMCPAuth:
    async def test_missing_token_raises_error(self, mcp_server):
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        with patch("atlassian_mcp.get_atlassian_token", side_effect=PermissionError("Authorization required.")):
            with patch("atlassian_mcp.get_cloud_id", return_value=CLOUD_ID):
                async with Client(mcp_server) as client:
                    with pytest.raises(ToolError, match="Authorization required"):
                        await client.call_tool("jira_search", {"target": "projects"})

    async def test_missing_cloud_id_raises_error(self, mcp_server):
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        with patch("atlassian_mcp.get_atlassian_token", return_value="token"):
            with patch("atlassian_mcp.get_cloud_id", side_effect=PermissionError("Cloud ID required.")):
                async with Client(mcp_server) as client:
                    with pytest.raises(ToolError, match="Cloud ID required"):
                        await client.call_tool("jira_search", {"target": "projects"})


# ---------------------------------------------------------------------------
# API error handling
# ---------------------------------------------------------------------------

class TestMCPErrorHandling:
    @respx.mock
    async def test_401_friendly_message(self, mcp_server):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(401, json=ATLASSIAN_ERROR_401)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "projects"})

        text = _get_text(result)
        assert "authentication failed" in text.lower()
        assert "reconnect" in text.lower()

    @respx.mock
    async def test_404_friendly_message(self, mcp_server):
        respx.get(f"{JIRA_BASE}/issue/BAD-999").mock(
            return_value=httpx.Response(404, json=ATLASSIAN_ERROR_404)
        )
        respx.get(f"{JIRA_BASE}/issue/BAD-999/comment").mock(
            return_value=httpx.Response(404, json=ATLASSIAN_ERROR_404)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_get", {"target": "issue", "issue_key": "BAD-999"}
                )

        text = _get_text(result)
        assert "not found" in text.lower()

    @respx.mock
    async def test_400_shows_validation_errors(self, mcp_server):
        respx.post(f"{JIRA_BASE}/issue").mock(
            return_value=httpx.Response(400, json=ATLASSIAN_ERROR_400_JIRA)
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "jira_manage",
                    {"target": "create_issue", "project_key": "BAD", "summary": "x"},
                )

        text = _get_text(result)
        assert "rejected" in text.lower()

    @respx.mock
    async def test_429_rate_limit(self, mcp_server):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(
                429,
                json=ATLASSIAN_ERROR_429,
                headers={"retry-after": "60"},
            )
        )
        with _mock_auth():
            from fastmcp import Client
            async with Client(mcp_server) as client:
                result = await client.call_tool("jira_search", {"target": "projects"})

        text = _get_text(result)
        assert "rate limit" in text.lower()
