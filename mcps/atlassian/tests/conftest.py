"""Shared fixtures and mock data for Atlassian API tests."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires cached Atlassian token)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--integration"):
        skip_integration = pytest.mark.skip(reason="needs --integration flag to run")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOUD_ID = "test-cloud-id-12345"
ATLASSIAN_API_BASE = "https://api.atlassian.com"
JIRA_BASE = f"{ATLASSIAN_API_BASE}/ex/jira/{CLOUD_ID}/rest/api/3"
CONFLUENCE_V2_BASE = f"{ATLASSIAN_API_BASE}/ex/confluence/{CLOUD_ID}/wiki/api/v2"
CONFLUENCE_V1_BASE = f"{ATLASSIAN_API_BASE}/ex/confluence/{CLOUD_ID}/wiki/rest/api"

# ---------------------------------------------------------------------------
# Jira: Projects
# ---------------------------------------------------------------------------

SAMPLE_PROJECT = {
    "id": "10001",
    "key": "PROJ",
    "name": "My Project",
    "projectTypeKey": "software",
    "simplified": False,
    "style": "classic",
}

SAMPLE_PROJECT_2 = {
    "id": "10002",
    "key": "HR",
    "name": "Human Resources",
    "projectTypeKey": "business",
    "simplified": True,
    "style": "next-gen",
}

SAMPLE_PROJECTS_RESPONSE = {
    "values": [SAMPLE_PROJECT, SAMPLE_PROJECT_2],
    "maxResults": 50,
    "total": 2,
    "isLast": True,
}

# ---------------------------------------------------------------------------
# Jira: Issues
# ---------------------------------------------------------------------------

SAMPLE_ISSUE = {
    "id": "10042",
    "key": "PROJ-42",
    "fields": {
        "summary": "Fix login timeout bug",
        "status": {"name": "In Progress", "id": "3"},
        "issuetype": {"name": "Bug", "id": "1"},
        "priority": {"name": "High", "id": "2"},
        "assignee": {
            "accountId": "5b10ac8d82e05b22cc7d4ef5",
            "displayName": "Alice Smith",
        },
        "reporter": {
            "accountId": "5b10a2844c20165700ede21g",
            "displayName": "Bob Jones",
        },
        "labels": ["backend", "auth"],
        "created": "2025-12-01T10:00:00.000+0000",
        "updated": "2025-12-10T14:00:00.000+0000",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Login times out after 30 seconds."}
                    ],
                }
            ],
        },
    },
    "renderedFields": {
        "description": "<p>Login times out after 30 seconds.</p>",
    },
}

SAMPLE_ISSUE_2 = {
    "id": "10043",
    "key": "PROJ-43",
    "fields": {
        "summary": "Add dark mode support",
        "status": {"name": "To Do", "id": "1"},
        "issuetype": {"name": "Story", "id": "2"},
        "priority": {"name": "Medium", "id": "3"},
        "assignee": None,
        "reporter": {
            "accountId": "5b10a2844c20165700ede21g",
            "displayName": "Bob Jones",
        },
        "labels": ["frontend", "ui"],
        "created": "2025-12-05T08:00:00.000+0000",
        "updated": "2025-12-05T08:00:00.000+0000",
        "description": None,
    },
    "renderedFields": {
        "description": "",
    },
}

SAMPLE_SEARCH_RESPONSE = {
    "issues": [SAMPLE_ISSUE, SAMPLE_ISSUE_2],
    "maxResults": 50,
    "isLast": True,
}

SAMPLE_SEARCH_RESPONSE_PAGINATED = {
    "issues": [SAMPLE_ISSUE],
    "maxResults": 1,
    "nextPageToken": "abc123token",
    "isLast": False,
}

SAMPLE_COUNT_RESPONSE = {"count": 42}

# ---------------------------------------------------------------------------
# Jira: Comments
# ---------------------------------------------------------------------------

SAMPLE_COMMENT = {
    "id": "10100",
    "body": {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "I can reproduce this on Chrome."}
                ],
            }
        ],
    },
    "author": {
        "accountId": "5b10a2844c20165700ede21g",
        "displayName": "Bob Jones",
    },
    "created": "2025-12-02T11:00:00.000+0000",
    "updated": "2025-12-02T11:00:00.000+0000",
}

SAMPLE_COMMENT_2 = {
    "id": "10101",
    "body": {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Working on a fix now."}
                ],
            }
        ],
    },
    "author": {
        "accountId": "5b10ac8d82e05b22cc7d4ef5",
        "displayName": "Alice Smith",
    },
    "created": "2025-12-03T09:00:00.000+0000",
    "updated": "2025-12-03T09:00:00.000+0000",
}

SAMPLE_COMMENTS_RESPONSE = {
    "comments": [SAMPLE_COMMENT, SAMPLE_COMMENT_2],
    "maxResults": 50,
    "total": 2,
    "startAt": 0,
}

SAMPLE_CREATED_COMMENT = {
    "id": "10200",
    "body": {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Great work!"}],
            }
        ],
    },
    "author": {
        "accountId": "5b10ac8d82e05b22cc7d4ef5",
        "displayName": "Alice Smith",
    },
    "created": "2025-12-15T10:00:00.000+0000",
}

# ---------------------------------------------------------------------------
# Jira: Create / Transition
# ---------------------------------------------------------------------------

SAMPLE_CREATED_ISSUE = {
    "id": "10100",
    "key": "PROJ-100",
    "self": f"{JIRA_BASE}/issue/10100",
}

SAMPLE_TRANSITIONS = {
    "transitions": [
        {"id": "11", "name": "To Do"},
        {"id": "21", "name": "In Progress"},
        {"id": "31", "name": "Done"},
    ]
}

# ---------------------------------------------------------------------------
# Confluence: Spaces
# ---------------------------------------------------------------------------

SAMPLE_SPACE = {
    "id": "65536",
    "key": "DEV",
    "name": "Development",
    "type": "global",
    "status": "current",
}

SAMPLE_SPACE_2 = {
    "id": "65537",
    "key": "HR",
    "name": "Human Resources",
    "type": "global",
    "status": "current",
}

SAMPLE_SPACES_RESPONSE = {
    "results": [SAMPLE_SPACE, SAMPLE_SPACE_2],
}

# ---------------------------------------------------------------------------
# Confluence: Pages
# ---------------------------------------------------------------------------

SAMPLE_PAGE = {
    "id": "12345",
    "title": "Architecture Overview",
    "status": "current",
    "spaceId": "65536",
    "createdAt": "2025-11-01T10:00:00.000Z",
    "version": {"number": 3, "createdAt": "2025-12-01T15:00:00.000Z"},
    "body": {
        "storage": {
            "representation": "storage",
            "value": "<h1>Architecture</h1><p>Our system uses microservices.</p>",
        }
    },
}

SAMPLE_CREATED_PAGE = {
    "id": "12400",
    "title": "New Page",
    "status": "current",
    "spaceId": "65536",
    "version": {"number": 1},
}

SAMPLE_UPDATED_PAGE = {
    "id": "12345",
    "title": "Architecture Overview v2",
    "status": "current",
    "spaceId": "65536",
    "version": {"number": 4},
}

# ---------------------------------------------------------------------------
# Confluence: Search
# ---------------------------------------------------------------------------

SAMPLE_SEARCH_RESULT = {
    "id": "12345",
    "type": "page",
    "status": "current",
    "title": "Architecture Overview",
}

SAMPLE_SEARCH_RESULT_2 = {
    "id": "12346",
    "type": "blogpost",
    "status": "current",
    "title": "Q4 Release Notes",
}

SAMPLE_CONFLUENCE_SEARCH_RESPONSE = {
    "results": [SAMPLE_SEARCH_RESULT, SAMPLE_SEARCH_RESULT_2],
}

# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

SAMPLE_USER = {
    "accountId": "5b10ac8d82e05b22cc7d4ef5",
    "displayName": "Alice Smith",
    "emailAddress": "alice@example.com",
    "active": True,
    "timeZone": "America/New_York",
    "avatarUrls": {
        "48x48": "https://avatar-management.example.com/alice.png",
    },
}

# ---------------------------------------------------------------------------
# Jira: Versions
# ---------------------------------------------------------------------------

SAMPLE_VERSION = {
    "id": "10300",
    "name": "1.0.0",
    "released": True,
    "releaseDate": "2025-06-01",
    "description": "First release",
    "projectId": 10001,
}

SAMPLE_VERSION_2 = {
    "id": "10301",
    "name": "1.1.0",
    "released": False,
    "releaseDate": "2026-09-01",
    "description": "Next release",
    "projectId": 10001,
}

SAMPLE_VERSIONS_RESPONSE = {
    "values": [SAMPLE_VERSION, SAMPLE_VERSION_2],
    "total": 2,
    "isLast": True,
}

SAMPLE_CREATED_VERSION = {
    "id": "10302",
    "name": "2.0.0",
    "released": False,
    "projectId": 10001,
    "self": f"{ATLASSIAN_API_BASE}/ex/jira/{CLOUD_ID}/rest/api/3/version/10302",
}

# ---------------------------------------------------------------------------
# Jira: Users
# ---------------------------------------------------------------------------

SAMPLE_USER_2 = {
    "accountId": "5b10a2844c20165700ede21g",
    "displayName": "Bob Jones",
    "emailAddress": "bob@example.com",
    "active": True,
    "timeZone": "America/Chicago",
}

SAMPLE_USERS_RESPONSE = [SAMPLE_USER, SAMPLE_USER_2]

# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------

ATLASSIAN_ERROR_401 = {
    "message": "Unauthorized",
}

ATLASSIAN_ERROR_403 = {
    "message": "Forbidden",
}

ATLASSIAN_ERROR_404 = {
    "errorMessages": ["Issue does not exist or you do not have permission to see it."],
    "errors": {},
}

ATLASSIAN_ERROR_400_JIRA = {
    "errorMessages": [],
    "errors": {
        "summary": "You must specify a summary of the issue.",
        "project": "project is required",
    },
}

ATLASSIAN_ERROR_429 = {
    "message": "Rate limit exceeded",
}
