"""Tests for Jira operations."""

import httpx
import pytest
import respx

from atlassian.atlassian_client import (
    AsyncAtlassianClient, AtlassianClient, AtlassianError,
)
from .conftest import (
    CLOUD_ID,
    JIRA_BASE,
    SAMPLE_PROJECTS_RESPONSE,
    SAMPLE_SEARCH_RESPONSE,
    SAMPLE_COUNT_RESPONSE,
    SAMPLE_ISSUE,
    SAMPLE_ISSUE_2,
    SAMPLE_COMMENTS_RESPONSE,
    SAMPLE_CREATED_ISSUE,
    SAMPLE_TRANSITIONS,
    SAMPLE_VERSIONS_RESPONSE,
    SAMPLE_CREATED_VERSION,
    SAMPLE_USERS_RESPONSE,
)

TOKEN = "test-token"


class TestListProjects:
    @respx.mock
    async def test_list_projects(self):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(200, json=SAMPLE_PROJECTS_RESPONSE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await jira.alist_projects(client)
        assert len(result) == 2
        assert result[0]["key"] == "PROJ"

    @respx.mock
    async def test_list_projects_empty(self):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(200, json={"values": [], "total": 0})
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await jira.alist_projects(client)
        assert result == []


class TestSearchIssues:
    @respx.mock
    async def test_search_issues(self):
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await jira.asearch_issues(client, jql="project = PROJ")
        issues = data["issues"]
        assert len(issues) == 2
        assert issues[0]["key"] == "PROJ-42"

    @respx.mock
    async def test_search_empty(self):
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [], "isLast": True})
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await jira.asearch_issues(client, jql="project = NONE")
        assert data["issues"] == []


class TestCountIssues:
    @respx.mock
    async def test_count_issues(self):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json=SAMPLE_COUNT_RESPONSE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            count = await jira.acount_issues(client, jql="project = PROJ")
        assert count == 42


class TestGetIssue:
    @respx.mock
    async def test_get_issue(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            issue = await jira.aget_issue(client, "PROJ-42")
        assert issue["key"] == "PROJ-42"
        assert issue["fields"]["summary"] == "Fix login timeout bug"


class TestGetIssueComments:
    @respx.mock
    async def test_get_comments(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/comment").mock(
            return_value=httpx.Response(200, json=SAMPLE_COMMENTS_RESPONSE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            comments = await jira.aget_issue_comments(client, "PROJ-42")
        assert len(comments) == 2
        assert comments[0]["author"]["displayName"] == "Bob Jones"


class TestCreateIssue:
    @respx.mock
    async def test_create_issue(self):
        route = respx.post(f"{JIRA_BASE}/issue").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await jira.acreate_issue(
                client, project_key="PROJ", summary="New bug"
            )
        assert result["key"] == "PROJ-100"
        # Verify the request payload
        body = route.calls[0].request.content
        assert b"PROJ" in body
        assert b"New bug" in body

    @respx.mock
    async def test_create_with_description(self):
        respx.post(f"{JIRA_BASE}/issue").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await jira.acreate_issue(
                client,
                project_key="PROJ",
                summary="Bug",
                description="Detailed description here",
                issue_type="Bug",
                priority="High",
                labels=["backend"],
            )
        assert result["key"] == "PROJ-100"


class TestUpdateIssue:
    @respx.mock
    async def test_update_issue(self):
        respx.put(f"{JIRA_BASE}/issue/PROJ-42").mock(
            return_value=httpx.Response(204)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            # Should not raise — 204 is success
            await jira.aupdate_issue(client, "PROJ-42", summary="Updated title")


class TestAddComment:
    @respx.mock
    async def test_add_comment(self):
        from .conftest import SAMPLE_CREATED_COMMENT
        respx.post(f"{JIRA_BASE}/issue/PROJ-42/comment").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_COMMENT)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await jira.aadd_issue_comment(client, "PROJ-42", "Great work!")
        assert result["id"] == "10200"


class TestTransitionIssue:
    @respx.mock
    async def test_transition_success(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json=SAMPLE_TRANSITIONS)
        )
        respx.post(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(204)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            matched = await jira.atransition_issue(client, "PROJ-42", "Done")
        assert matched == "Done"

    @respx.mock
    async def test_transition_case_insensitive(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json=SAMPLE_TRANSITIONS)
        )
        respx.post(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(204)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            matched = await jira.atransition_issue(client, "PROJ-42", "in progress")
        assert matched == "In Progress"

    @respx.mock
    async def test_transition_invalid_name(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json=SAMPLE_TRANSITIONS)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            with pytest.raises(AtlassianError, match="not available"):
                await jira.atransition_issue(client, "PROJ-42", "Invalid Status")


# ---------------------------------------------------------------------------
# search_all_issues pagination
# ---------------------------------------------------------------------------

class TestSearchAllIssues:
    @respx.mock
    async def test_single_page(self):
        """When isLast=True on first page, returns those issues only."""
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json={
                "issues": [SAMPLE_ISSUE],
                "isLast": True,
            })
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            issues = await jira.asearch_all_issues(client, "project = PROJ")
        assert len(issues) == 1
        assert issues[0]["key"] == "PROJ-42"

    @respx.mock
    async def test_multiple_pages(self):
        """Follows nextPageToken across two pages."""
        page1 = {
            "issues": [SAMPLE_ISSUE],
            "isLast": False,
            "nextPageToken": "page2token",
        }
        page2 = {
            "issues": [SAMPLE_ISSUE_2],
            "isLast": True,
        }
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            side_effect=[
                httpx.Response(200, json=page1),
                httpx.Response(200, json=page2),
            ]
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            issues = await jira.asearch_all_issues(client, "project = PROJ")
        assert len(issues) == 2
        assert issues[0]["key"] == "PROJ-42"
        assert issues[1]["key"] == "PROJ-43"

    @respx.mock
    async def test_max_total_truncates(self):
        """Stops fetching and truncates when max_total is reached."""
        page1 = {
            "issues": [SAMPLE_ISSUE, SAMPLE_ISSUE_2],
            "isLast": False,
            "nextPageToken": "more",
        }
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json=page1)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            issues = await jira.asearch_all_issues(
                client, "project = PROJ", max_total=1,
            )
        assert len(issues) == 1

    @respx.mock
    async def test_empty_next_page_token_stops(self):
        """Stops if nextPageToken is missing even when isLast=False."""
        page1 = {
            "issues": [SAMPLE_ISSUE],
            "isLast": False,
            # No nextPageToken
        }
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json=page1)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            issues = await jira.asearch_all_issues(client, "project = PROJ")
        assert len(issues) == 1

    @respx.mock
    async def test_empty_issues_stops(self):
        """Stops if API returns empty issues array."""
        page1 = {
            "issues": [],
            "isLast": False,
            "nextPageToken": "shouldnotfollow",
        }
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json=page1)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            issues = await jira.asearch_all_issues(client, "project = PROJ")
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Project Versions
# ---------------------------------------------------------------------------

class TestGetProjectVersions:
    @respx.mock
    async def test_get_all_versions(self):
        respx.get(f"{JIRA_BASE}/project/PROJ/version").mock(
            return_value=httpx.Response(200, json=SAMPLE_VERSIONS_RESPONSE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            versions = await jira.aget_project_versions(client, "PROJ")
        assert len(versions) == 2
        assert versions[0]["name"] == "1.0.0"
        assert versions[1]["name"] == "1.1.0"

    @respx.mock
    async def test_get_versions_status_filter(self):
        route = respx.get(f"{JIRA_BASE}/project/PROJ/version").mock(
            return_value=httpx.Response(200, json={"values": [SAMPLE_VERSIONS_RESPONSE["values"][0]], "isLast": True})
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            versions = await jira.aget_project_versions(client, "PROJ", status="released")
        assert len(versions) == 1
        assert "status=released" in str(route.calls[0].request.url)


class TestCreateVersion:
    @respx.mock
    async def test_create_version(self):
        route = respx.post(f"{JIRA_BASE}/version").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_VERSION)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await jira.acreate_version(
                client,
                project_key="PROJ",
                name="2.0.0",
                description="Major release",
                release_date="2026-12-01",
            )
        assert result["id"] == "10302"
        assert result["name"] == "2.0.0"
        body = route.calls[0].request.content
        assert b"PROJ" in body
        assert b"2.0.0" in body


# ---------------------------------------------------------------------------
# Search Users
# ---------------------------------------------------------------------------

class TestSearchUsers:
    @respx.mock
    async def test_search_users(self):
        respx.get(f"{JIRA_BASE}/user/search").mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_RESPONSE)
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            users = await jira.asearch_users(client, "alice")
        assert len(users) == 2
        assert users[0]["displayName"] == "Alice Smith"
        assert users[0]["accountId"] == "5b10ac8d82e05b22cc7d4ef5"

    @respx.mock
    async def test_search_users_empty(self):
        respx.get(f"{JIRA_BASE}/user/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        from atlassian import jira
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            users = await jira.asearch_users(client, "nobody")
        assert users == []


# ---------------------------------------------------------------------------
# ADF mention support
# ---------------------------------------------------------------------------

class TestTextToAdfMentions:
    def test_plain_text_unchanged(self):
        from atlassian.jira import _text_to_adf
        adf = _text_to_adf("Hello world")
        content = adf["content"][0]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "Hello world"

    def test_mention_produces_mention_node(self):
        from atlassian.jira import _text_to_adf
        adf = _text_to_adf("Hi @{abc123} please review")
        content = adf["content"][0]["content"]
        assert len(content) == 3
        assert content[0] == {"type": "text", "text": "Hi "}
        assert content[1]["type"] == "mention"
        assert content[1]["attrs"]["id"] == "abc123"
        assert content[2] == {"type": "text", "text": " please review"}

    def test_multiple_mentions(self):
        from atlassian.jira import _text_to_adf
        adf = _text_to_adf("@{user1} and @{user2}")
        content = adf["content"][0]["content"]
        mention_nodes = [c for c in content if c["type"] == "mention"]
        assert len(mention_nodes) == 2
        assert mention_nodes[0]["attrs"]["id"] == "user1"
        assert mention_nodes[1]["attrs"]["id"] == "user2"

    def test_mention_only(self):
        from atlassian.jira import _text_to_adf
        adf = _text_to_adf("@{onlyuser}")
        content = adf["content"][0]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "mention"
        assert content[0]["attrs"]["id"] == "onlyuser"


# ---------------------------------------------------------------------------
# Sync client operations (M3/M9 from review)
# ---------------------------------------------------------------------------

class TestSyncOperations:
    @respx.mock
    def test_sync_list_projects(self):
        respx.get(f"{JIRA_BASE}/project/search").mock(
            return_value=httpx.Response(200, json=SAMPLE_PROJECTS_RESPONSE)
        )
        from atlassian import jira
        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            result = jira.list_projects(client)
        assert len(result) == 2

    @respx.mock
    def test_sync_search_issues(self):
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        from atlassian import jira
        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            data = jira.search_issues(client, "project = PROJ")
        assert len(data["issues"]) == 2

    @respx.mock
    def test_sync_count_issues(self):
        respx.post(f"{JIRA_BASE}/search/approximate-count").mock(
            return_value=httpx.Response(200, json=SAMPLE_COUNT_RESPONSE)
        )
        from atlassian import jira
        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            count = jira.count_issues(client, "project = PROJ")
        assert count == 42

    @respx.mock
    def test_sync_search_all_issues(self):
        """Sync version also paginates correctly."""
        page1 = {
            "issues": [SAMPLE_ISSUE],
            "isLast": False,
            "nextPageToken": "tok2",
        }
        page2 = {
            "issues": [SAMPLE_ISSUE_2],
            "isLast": True,
        }
        respx.get(f"{JIRA_BASE}/search/jql").mock(
            side_effect=[
                httpx.Response(200, json=page1),
                httpx.Response(200, json=page2),
            ]
        )
        from atlassian import jira
        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            issues = jira.search_all_issues(client, "project = PROJ")
        assert len(issues) == 2

    @respx.mock
    def test_sync_get_issue(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42").mock(
            return_value=httpx.Response(200, json=SAMPLE_ISSUE)
        )
        from atlassian import jira
        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            issue = jira.get_issue(client, "PROJ-42")
        assert issue["key"] == "PROJ-42"

    @respx.mock
    def test_sync_create_issue(self):
        respx.post(f"{JIRA_BASE}/issue").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ISSUE)
        )
        from atlassian import jira
        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            result = jira.create_issue(client, "PROJ", "New bug")
        assert result["key"] == "PROJ-100"

    @respx.mock
    def test_sync_transition_issue(self):
        respx.get(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(200, json=SAMPLE_TRANSITIONS)
        )
        respx.post(f"{JIRA_BASE}/issue/PROJ-42/transitions").mock(
            return_value=httpx.Response(204)
        )
        from atlassian import jira
        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            matched = jira.transition_issue(client, "PROJ-42", "Done")
        assert matched == "Done"
