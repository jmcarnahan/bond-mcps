#!/usr/bin/env python3
"""
Integration tests for the Atlassian CLI.

These tests hit the real Atlassian API using locally cached credentials.
They require:
  1. A valid cached token at ~/.bond_ai_tokens/atlassian.json
  2. ATLASSIAN_CLIENT_ID and ATLASSIAN_CLIENT_SECRET env vars (or .env file)

Run:
    cd mcps/atlassian
    poetry run pytest tests/test_integration_cli.py -v -s

Skip with:
    poetry run pytest tests/ --ignore=tests/test_integration_cli.py
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

TOKEN_CACHE = Path.home() / ".bond_ai_tokens" / "atlassian.json"


def _has_credentials() -> bool:
    """Check if we have credentials to run integration tests."""
    if not TOKEN_CACHE.exists():
        return False
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    return bool(os.environ.get("ATLASSIAN_CLIENT_ID"))


def _skip_if_no_creds():
    if not _has_credentials():
        pytest.skip("No cached Atlassian token or ATLASSIAN_CLIENT_ID not set")


@pytest.fixture(autouse=True)
def check_credentials():
    _skip_if_no_creds()


@pytest.fixture
def client():
    """Get an authenticated AtlassianClient."""
    from dotenv import load_dotenv
    load_dotenv()
    from atlassian.local_auth import get_local_token_and_cloud_id
    from atlassian.atlassian_client import AtlassianClient
    token, cloud_id = get_local_token_and_cloud_id()
    with AtlassianClient(token, cloud_id) as c:
        yield c


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class TestUserIntegration:
    def test_get_myself(self, client):
        from atlassian import user as user_ops
        result = user_ops.get_myself(client)
        assert result.get("accountId"), "Expected accountId"
        assert result.get("displayName"), "Expected displayName"
        print(f"  User: {result['displayName']} ({result['accountId']})")


# ---------------------------------------------------------------------------
# Jira: Projects
# ---------------------------------------------------------------------------

class TestJiraProjectsIntegration:
    def test_list_projects(self, client):
        from atlassian import jira
        projects = jira.list_projects(client, max_results=5)
        assert isinstance(projects, list)
        assert len(projects) > 0
        print(f"  Projects: {[p['key'] for p in projects]}")

    def test_get_project_versions(self, client):
        from atlassian import jira
        projects = jira.list_projects(client, max_results=1)
        key = projects[0]["key"]
        versions = jira.get_project_versions(client, key)
        assert isinstance(versions, list)
        print(f"  Versions for {key}: {len(versions)}")


# ---------------------------------------------------------------------------
# Jira: Issues
# ---------------------------------------------------------------------------

class TestJiraIssuesIntegration:
    def _get_project_key(self, client):
        from atlassian import jira
        projects = jira.list_projects(client, max_results=1)
        return projects[0]["key"]

    def test_search_issues(self, client):
        from atlassian import jira
        key = self._get_project_key(client)
        issues = jira.search_all_issues(client, jql=f"project = {key} ORDER BY created DESC", max_total=3)
        assert isinstance(issues, list)
        assert len(issues) > 0
        for i in issues:
            print(f"  {i['key']}: {i['fields']['summary']}")

    def test_count_issues(self, client):
        from atlassian import jira
        key = self._get_project_key(client)
        count = jira.count_issues(client, jql=f"project = {key}")
        assert count > 0
        print(f"  Total issues in {key}: {count}")

    def test_get_issue(self, client):
        from atlassian import jira
        key = self._get_project_key(client)
        issues = jira.search_all_issues(client, jql=f"project = {key} ORDER BY created DESC", max_total=1)
        issue_key = issues[0]["key"]
        issue = jira.get_issue(client, issue_key)
        assert issue["key"] == issue_key
        assert issue["fields"]["summary"]
        print(f"  Got: {issue_key} — {issue['fields']['summary']}")

    def test_get_issue_comments(self, client):
        from atlassian import jira
        key = self._get_project_key(client)
        issues = jira.search_all_issues(client, jql=f"project = {key} ORDER BY created DESC", max_total=1)
        issue_key = issues[0]["key"]
        comments = jira.get_issue_comments(client, issue_key)
        assert isinstance(comments, list)
        print(f"  Comments on {issue_key}: {len(comments)}")


# ---------------------------------------------------------------------------
# Jira: Transitions
# ---------------------------------------------------------------------------

class TestJiraTransitionsIntegration:
    def test_get_transitions(self, client):
        from atlassian import jira
        projects = jira.list_projects(client, max_results=1)
        proj_key = projects[0]["key"]
        issues = jira.search_all_issues(client, jql=f"project = {proj_key} ORDER BY created DESC", max_total=1)
        key = issues[0]["key"]
        transitions = jira.get_transitions(client, key)
        assert isinstance(transitions, list)
        assert len(transitions) > 0
        names = [t["name"] for t in transitions]
        print(f"  Transitions for {key}: {names}")


# ---------------------------------------------------------------------------
# Jira: Versions
# ---------------------------------------------------------------------------

class TestJiraVersionsIntegration:
    def test_create_and_list_versions(self, client):
        import time
        from atlassian import jira
        projects = jira.list_projects(client, max_results=1)
        key = projects[0]["key"]

        version_name = f"integ-test-{int(time.time())}"
        version = jira.create_version(client, project_key=key, name=version_name)
        assert version.get("id")
        assert version["name"] == version_name
        print(f"  Created version: {version['name']} (ID: {version['id']})")

        versions = jira.get_project_versions(client, key)
        found = any(v["name"] == version_name for v in versions)
        assert found, "Created version should appear in list"

    def test_get_versions_with_status_filter(self, client):
        from atlassian import jira
        projects = jira.list_projects(client, max_results=1)
        key = projects[0]["key"]
        unreleased = jira.get_project_versions(client, key, status="unreleased")
        assert isinstance(unreleased, list)
        print(f"  Unreleased versions for {key}: {len(unreleased)}")


# ---------------------------------------------------------------------------
# Jira: User search
# ---------------------------------------------------------------------------

class TestJiraUserSearchIntegration:
    def test_search_users(self, client):
        from atlassian import jira
        users = jira.search_users(client, "john")
        assert isinstance(users, list)
        assert len(users) > 0
        for u in users:
            print(f"  {u.get('displayName', '?')} — {u.get('accountId', '?')}")


# ---------------------------------------------------------------------------
# Jira: Comments with mentions
# ---------------------------------------------------------------------------

class TestJiraCommentIntegration:
    def test_add_comment_with_mention(self, client):
        from atlassian import jira
        projects = jira.list_projects(client, max_results=1)
        proj_key = projects[0]["key"]
        issues = jira.search_all_issues(client, jql=f"project = {proj_key} ORDER BY created DESC", max_total=1)
        key = issues[0]["key"]

        users = jira.search_users(client, "john")
        account_id = users[0]["accountId"]

        result = jira.add_issue_comment(
            client, key,
            f"Integration test: mentioning @{{{account_id}}}",
            author_label="Integration Test",
        )
        assert result.get("id")
        print(f"  Comment added to {key} with mention (ID: {result['id']})")


# ---------------------------------------------------------------------------
# Confluence: Spaces
# ---------------------------------------------------------------------------

class TestConfluenceIntegration:
    def test_list_spaces(self, client):
        from atlassian import confluence
        spaces = confluence.list_spaces(client)
        assert isinstance(spaces, list)
        assert len(spaces) > 0
        for s in spaces:
            print(f"  {s.get('key', '?')}: {s.get('name', '?')}")

    def test_search_content(self, client):
        from atlassian import confluence
        results = confluence.search_content(client, query="type = page")
        assert isinstance(results, list)
        assert len(results) > 0
        for r in results:
            print(f"  {r.get('id', '?')}: {r.get('title', '?')}")

    def test_get_page(self, client):
        from atlassian import confluence
        results = confluence.search_content(client, query="type = page", max_results=1)
        page_id = results[0]["id"]
        page = confluence.get_page(client, page_id)
        assert page.get("title")
        assert page.get("body")
        print(f"  Page: {page['title']} (version {page['version']['number']})")

    def test_full_text_search(self, client):
        from atlassian import confluence
        import re
        pages = confluence.search_content(client, query="type = page", max_results=1)
        page = confluence.get_page(client, pages[0]["id"])
        body = page.get("body", {}).get("storage", {}).get("value", "")
        # Strip HTML/XML tags and pick a plain word
        plain = re.sub(r"<[^>]+>", " ", body)
        words = [w for w in plain.split() if len(w) > 6 and w.isalpha() and w.islower()]
        if not words:
            pytest.skip("No searchable words in page body")
        search_word = words[0]
        results = confluence.search_content(client, query=f'type = page AND text ~ "{search_word}"')
        assert len(results) > 0, f"Expected to find pages containing '{search_word}'"
        print(f"  Full-text search for '{search_word}': {len(results)} result(s)")
