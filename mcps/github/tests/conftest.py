"""Shared fixtures and mock data for GitHub API tests."""

import pytest

# ---------------------------------------------------------------------------
# Sample GitHub API response payloads
# ---------------------------------------------------------------------------

SAMPLE_REPO = {
    "id": 123456,
    "name": "my-project",
    "full_name": "octocat/my-project",
    "private": False,
    "description": "A sample project",
    "language": "Python",
    "stargazers_count": 42,
    "forks_count": 10,
    "open_issues_count": 5,
    "default_branch": "main",
    "created_at": "2024-01-15T10:00:00Z",
    "updated_at": "2025-12-01T15:30:00Z",
    "html_url": "https://github.com/octocat/my-project",
    "owner": {
        "login": "octocat",
        "id": 1,
    },
}

SAMPLE_REPO_PRIVATE = {
    "id": 789012,
    "name": "secret-project",
    "full_name": "octocat/secret-project",
    "private": True,
    "description": "A private project",
    "language": "TypeScript",
    "stargazers_count": 0,
    "forks_count": 0,
    "open_issues_count": 2,
    "default_branch": "main",
    "created_at": "2025-06-01T08:00:00Z",
    "updated_at": "2025-12-15T12:00:00Z",
    "html_url": "https://github.com/octocat/secret-project",
    "owner": {
        "login": "octocat",
        "id": 1,
    },
}

SAMPLE_REPOS_RESPONSE = [SAMPLE_REPO, SAMPLE_REPO_PRIVATE]

SAMPLE_SEARCH_REPOS_RESPONSE = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [SAMPLE_REPO, SAMPLE_REPO_PRIVATE],
}

# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

SAMPLE_ISSUE = {
    "id": 1001,
    "number": 42,
    "title": "Fix login bug",
    "state": "open",
    "body": "The login form crashes when email contains a plus sign.",
    "created_at": "2025-12-01T10:00:00Z",
    "updated_at": "2025-12-10T14:00:00Z",
    "comments": 3,
    "html_url": "https://github.com/octocat/my-project/issues/42",
    "user": {"login": "octocat", "id": 1},
    "labels": [
        {"id": 1, "name": "bug", "color": "d73a4a"},
        {"id": 2, "name": "high-priority", "color": "ff0000"},
    ],
    "assignees": [{"login": "octocat", "id": 1}],
    "assignee": {"login": "octocat", "id": 1},
}

SAMPLE_ISSUE_2 = {
    "id": 1002,
    "number": 43,
    "title": "Add dark mode support",
    "state": "open",
    "body": "We should add dark mode to the UI.",
    "created_at": "2025-12-05T08:00:00Z",
    "updated_at": "2025-12-05T08:00:00Z",
    "comments": 0,
    "html_url": "https://github.com/octocat/my-project/issues/43",
    "user": {"login": "contributor", "id": 2},
    "labels": [{"id": 3, "name": "enhancement", "color": "a2eeef"}],
    "assignees": [],
    "assignee": None,
}

SAMPLE_ISSUE_PR = {
    "id": 1003,
    "number": 44,
    "title": "Fix typo in README",
    "state": "open",
    "pull_request": {"url": "https://api.github.com/repos/octocat/my-project/pulls/44"},
    "html_url": "https://github.com/octocat/my-project/pull/44",
    "user": {"login": "contributor", "id": 2},
    "labels": [],
    "assignees": [],
    "assignee": None,
    "comments": 0,
    "created_at": "2025-12-10T08:00:00Z",
    "updated_at": "2025-12-10T08:00:00Z",
}

SAMPLE_ISSUES_RESPONSE = [SAMPLE_ISSUE, SAMPLE_ISSUE_2, SAMPLE_ISSUE_PR]

SAMPLE_COMMENT = {
    "id": 2001,
    "body": "I can reproduce this issue on Chrome.",
    "created_at": "2025-12-02T11:00:00Z",
    "updated_at": "2025-12-02T11:00:00Z",
    "html_url": "https://github.com/octocat/my-project/issues/42#issuecomment-2001",
    "user": {"login": "contributor", "id": 2},
}

SAMPLE_COMMENT_2 = {
    "id": 2002,
    "body": "Working on a fix now.",
    "created_at": "2025-12-03T09:00:00Z",
    "updated_at": "2025-12-03T09:00:00Z",
    "html_url": "https://github.com/octocat/my-project/issues/42#issuecomment-2002",
    "user": {"login": "octocat", "id": 1},
}

SAMPLE_COMMENTS_RESPONSE = [SAMPLE_COMMENT, SAMPLE_COMMENT_2]

SAMPLE_CREATED_ISSUE = {
    "id": 1010,
    "number": 50,
    "title": "New feature request",
    "state": "open",
    "body": "Please add this feature.",
    "html_url": "https://github.com/octocat/my-project/issues/50",
    "user": {"login": "octocat", "id": 1},
    "labels": [],
    "assignees": [],
}

SAMPLE_CREATED_COMMENT = {
    "id": 2010,
    "body": "This is a new comment.",
    "html_url": "https://github.com/octocat/my-project/issues/42#issuecomment-2010",
    "user": {"login": "octocat", "id": 1},
    "created_at": "2025-12-15T10:00:00Z",
}

# ---------------------------------------------------------------------------
# Pull Requests
# ---------------------------------------------------------------------------

SAMPLE_PR = {
    "id": 3001,
    "number": 101,
    "title": "Add login validation",
    "state": "open",
    "draft": False,
    "body": "This PR adds email validation to the login form.",
    "created_at": "2025-12-10T09:00:00Z",
    "updated_at": "2025-12-12T16:00:00Z",
    "html_url": "https://github.com/octocat/my-project/pull/101",
    "user": {"login": "octocat", "id": 1},
    "head": {"ref": "feature/login-validation", "sha": "abc123"},
    "base": {"ref": "main", "sha": "def456"},
    "mergeable": True,
    "changed_files": 3,
    "additions": 45,
    "deletions": 12,
    "commits": 2,
}

SAMPLE_PR_DRAFT = {
    "id": 3002,
    "number": 102,
    "title": "WIP: Dark mode",
    "state": "open",
    "draft": True,
    "body": "Work in progress dark mode implementation.",
    "created_at": "2025-12-11T10:00:00Z",
    "updated_at": "2025-12-11T10:00:00Z",
    "html_url": "https://github.com/octocat/my-project/pull/102",
    "user": {"login": "contributor", "id": 2},
    "head": {"ref": "feature/dark-mode", "sha": "ghi789"},
    "base": {"ref": "main", "sha": "def456"},
    "mergeable": None,
    "changed_files": 8,
    "additions": 200,
    "deletions": 50,
    "commits": 5,
}

SAMPLE_PRS_RESPONSE = [SAMPLE_PR, SAMPLE_PR_DRAFT]

SAMPLE_CREATED_PR = {
    "id": 3010,
    "number": 110,
    "title": "Fix bug in parser",
    "state": "open",
    "draft": False,
    "body": "Fixes the parser crash.",
    "html_url": "https://github.com/octocat/my-project/pull/110",
    "user": {"login": "octocat", "id": 1},
    "head": {"ref": "fix/parser", "sha": "xyz123"},
    "base": {"ref": "main", "sha": "def456"},
}

SAMPLE_MERGE_RESULT = {
    "sha": "abc123def456",
    "merged": True,
    "message": "Pull Request successfully merged",
}

# ---------------------------------------------------------------------------
# Code & Content
# ---------------------------------------------------------------------------

SAMPLE_FILE_CONTENT = {
    "name": "main.py",
    "path": "src/main.py",
    "sha": "abc123",
    "size": 256,
    "encoding": "base64",
    "content": "aW1wb3J0IHN5cwoKZGVmIG1haW4oKToKICAgIHByaW50KCJIZWxsbyBXb3JsZCIpCg==",
    "html_url": "https://github.com/octocat/my-project/blob/main/src/main.py",
    "download_url": "https://raw.githubusercontent.com/octocat/my-project/main/src/main.py",
}

SAMPLE_DIRECTORY_LISTING = [
    {"name": "main.py", "path": "src/main.py", "type": "file", "size": 256},
    {"name": "utils.py", "path": "src/utils.py", "type": "file", "size": 128},
    {"name": "tests", "path": "src/tests", "type": "dir", "size": 0},
]

SAMPLE_FILE_CREATE_RESULT = {
    "content": {
        "name": "new-file.txt",
        "path": "new-file.txt",
        "sha": "new123",
        "html_url": "https://github.com/octocat/my-project/blob/main/new-file.txt",
    },
    "commit": {
        "sha": "commit456",
        "message": "Create new-file.txt",
    },
}

SAMPLE_CODE_SEARCH_RESPONSE = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [
        {
            "name": "main.py",
            "path": "src/main.py",
            "sha": "abc123",
            "html_url": "https://github.com/octocat/my-project/blob/main/src/main.py",
            "repository": {"full_name": "octocat/my-project"},
        },
        {
            "name": "app.py",
            "path": "src/app.py",
            "sha": "def456",
            "html_url": "https://github.com/octocat/other-project/blob/main/src/app.py",
            "repository": {"full_name": "octocat/other-project"},
        },
    ],
}

# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

SAMPLE_USER = {
    "login": "octocat",
    "id": 1,
    "name": "The Octocat",
    "email": "octocat@github.com",
    "bio": "I love coding!",
    "company": "GitHub",
    "location": "San Francisco",
    "public_repos": 42,
    "followers": 1000,
    "following": 50,
    "html_url": "https://github.com/octocat",
    "created_at": "2008-01-14T04:33:35Z",
}

# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------

GITHUB_ERROR_401 = {
    "message": "Bad credentials",
    "documentation_url": "https://docs.github.com/rest",
}

GITHUB_ERROR_404 = {
    "message": "Not Found",
    "documentation_url": "https://docs.github.com/rest",
}

GITHUB_ERROR_422 = {
    "message": "Validation Failed",
    "errors": [{"resource": "Issue", "code": "missing_field", "field": "title", "message": "title is missing"}],
    "documentation_url": "https://docs.github.com/rest",
}
