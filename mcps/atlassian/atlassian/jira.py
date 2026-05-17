"""
Jira operations — sync and async pairs.

Uses Jira REST API v3 via the Atlassian cloud gateway.
Search uses the new /search/jql endpoint (the old /search is deprecated).
Count uses the dedicated /search/approximate-count endpoint.
"""

import re
from typing import Any, Dict, List, Optional

from .atlassian_client import AtlassianClient, AsyncAtlassianClient


def _cap(value: int, maximum: int = 100) -> int:
    """Clamp a pagination value between 1 and maximum."""
    return min(max(value, 1), maximum)


# Matches @{accountId} mention syntax in comment/description text.
_MENTION_RE = re.compile(r"@\{([^}]+)\}")


def _line_to_adf_content(line: str) -> List[Dict[str, Any]]:
    """Convert a single line of text (possibly with @{accountId} mentions) to ADF content nodes."""
    if not line:
        return []

    content: List[Dict[str, Any]] = []
    last_end = 0
    for match in _MENTION_RE.finditer(line):
        if match.start() > last_end:
            content.append({"type": "text", "text": line[last_end:match.start()]})
        account_id = match.group(1)
        content.append({
            "type": "mention",
            "attrs": {
                "id": account_id,
                "text": f"@{account_id}",
                "accessLevel": "SITE",
            },
        })
        last_end = match.end()
    if last_end < len(line):
        content.append({"type": "text", "text": line[last_end:]})
    return content


def _text_to_adf(text: str) -> Dict[str, Any]:
    """Wrap plain text in minimal Atlassian Document Format.

    Splits on newlines to produce multiple paragraphs.
    Supports @{accountId} syntax for user mentions.
    Empty input produces a doc with a single empty paragraph.
    """
    lines = text.split("\n") if text else [""]
    paragraphs = [
        {
            "type": "paragraph",
            "content": _line_to_adf_content(line),
        }
        for line in lines
    ]
    return {
        "type": "doc",
        "version": 1,
        "content": paragraphs,
    }


# ---------------------------------------------------------------------------
# List Projects
# ---------------------------------------------------------------------------

def list_projects(
    client: AtlassianClient,
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """List accessible Jira projects."""
    data = client.get(
        f"{client.jira_base}/project/search",
        params={"maxResults": _cap(max_results), "orderBy": "name"},
    )
    return data.get("values", [])


async def alist_projects(
    client: AsyncAtlassianClient,
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """List accessible Jira projects (async)."""
    data = await client.get(
        f"{client.jira_base}/project/search",
        params={"maxResults": _cap(max_results), "orderBy": "name"},
    )
    return data.get("values", [])


# ---------------------------------------------------------------------------
# Search Issues (new /search/jql endpoint)
# ---------------------------------------------------------------------------

# Bare minimum fields — key is always included by the API regardless.
_SEARCH_FIELDS_MINIMAL = "summary,status,issuetype"

# Extended fields available on request.
_SEARCH_FIELDS_EXTENDED = (
    "summary,status,issuetype,assignee,priority,created,updated,labels"
)

# Maximum issues per page (Jira hard limit is 100)
_PAGE_SIZE = 100

# Safety cap — refuse to fetch more than this to prevent runaway queries
_MAX_TOTAL_ISSUES = 10_000


def search_issues(
    client: AtlassianClient,
    jql: str,
    max_results: int = 50,
    fields: str = _SEARCH_FIELDS_MINIMAL,
    next_page_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Search issues using JQL (single page). Returns raw API response."""
    params: Dict[str, Any] = {
        "jql": jql,
        "maxResults": _cap(max_results),
        "fields": fields,
    }
    if next_page_token:
        params["nextPageToken"] = next_page_token
    return client.get(f"{client.jira_base}/search/jql", params=params)


async def asearch_issues(
    client: AsyncAtlassianClient,
    jql: str,
    max_results: int = 50,
    fields: str = _SEARCH_FIELDS_MINIMAL,
    next_page_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Search issues using JQL (single page, async). Returns raw API response."""
    params: Dict[str, Any] = {
        "jql": jql,
        "maxResults": _cap(max_results),
        "fields": fields,
    }
    if next_page_token:
        params["nextPageToken"] = next_page_token
    return await client.get(f"{client.jira_base}/search/jql", params=params)


def search_all_issues(
    client: AtlassianClient,
    jql: str,
    fields: str = _SEARCH_FIELDS_MINIMAL,
    max_total: int = _MAX_TOTAL_ISSUES,
) -> List[Dict[str, Any]]:
    """Fetch ALL issues matching JQL, auto-paginating in batches of 100.

    Returns a flat list of issue dicts. Stops after max_total issues as a
    safety cap.
    """
    all_issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None

    while len(all_issues) < max_total:
        batch = min(_PAGE_SIZE, max_total - len(all_issues))
        data = search_issues(
            client, jql,
            max_results=batch,
            fields=fields,
            next_page_token=next_page_token,
        )
        issues = data.get("issues", [])
        all_issues.extend(issues)

        if data.get("isLast", True) or not issues:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return all_issues[:max_total]


async def asearch_all_issues(
    client: AsyncAtlassianClient,
    jql: str,
    fields: str = _SEARCH_FIELDS_MINIMAL,
    max_total: int = _MAX_TOTAL_ISSUES,
) -> List[Dict[str, Any]]:
    """Fetch ALL issues matching JQL, auto-paginating in batches of 100 (async).

    Returns a flat list of issue dicts. Stops after max_total issues as a
    safety cap.
    """
    all_issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None

    while len(all_issues) < max_total:
        batch = min(_PAGE_SIZE, max_total - len(all_issues))
        data = await asearch_issues(
            client, jql,
            max_results=batch,
            fields=fields,
            next_page_token=next_page_token,
        )
        issues = data.get("issues", [])
        all_issues.extend(issues)

        if data.get("isLast", True) or not issues:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return all_issues[:max_total]


# ---------------------------------------------------------------------------
# Count Issues
# ---------------------------------------------------------------------------

def count_issues(
    client: AtlassianClient,
    jql: str,
) -> int:
    """Get approximate count of issues matching JQL."""
    data = client.post(
        f"{client.jira_base}/search/approximate-count",
        json_data={"jql": jql},
    )
    return data.get("count", 0) if data else 0


async def acount_issues(
    client: AsyncAtlassianClient,
    jql: str,
) -> int:
    """Get approximate count of issues matching JQL (async)."""
    data = await client.post(
        f"{client.jira_base}/search/approximate-count",
        json_data={"jql": jql},
    )
    return data.get("count", 0) if data else 0


# ---------------------------------------------------------------------------
# Get Issue
# ---------------------------------------------------------------------------

def get_issue(
    client: AtlassianClient,
    issue_key: str,
    expand: str = "renderedFields",
) -> Dict[str, Any]:
    """Get full issue details including rendered fields."""
    return client.get(
        f"{client.jira_base}/issue/{issue_key}",
        params={"expand": expand},
    )


async def aget_issue(
    client: AsyncAtlassianClient,
    issue_key: str,
    expand: str = "renderedFields",
) -> Dict[str, Any]:
    """Get full issue details including rendered fields (async)."""
    return await client.get(
        f"{client.jira_base}/issue/{issue_key}",
        params={"expand": expand},
    )


# ---------------------------------------------------------------------------
# Get Issue Comments
# ---------------------------------------------------------------------------

def get_issue_comments(
    client: AtlassianClient,
    issue_key: str,
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """Get comments for an issue."""
    data = client.get(
        f"{client.jira_base}/issue/{issue_key}/comment",
        params={"maxResults": _cap(max_results), "orderBy": "created"},
    )
    return data.get("comments", [])


async def aget_issue_comments(
    client: AsyncAtlassianClient,
    issue_key: str,
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """Get comments for an issue (async)."""
    data = await client.get(
        f"{client.jira_base}/issue/{issue_key}/comment",
        params={"maxResults": _cap(max_results), "orderBy": "created"},
    )
    return data.get("comments", [])


# ---------------------------------------------------------------------------
# Create Issue
# ---------------------------------------------------------------------------

def create_issue(
    client: AtlassianClient,
    project_key: str,
    summary: str,
    issue_type: str = "Task",
    description: Optional[str] = None,
    assignee_id: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new Jira issue."""
    fields: Dict[str, Any] = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"name": issue_type},
    }
    if description:
        fields["description"] = _text_to_adf(description)
    if assignee_id:
        fields["assignee"] = {"accountId": assignee_id}
    if priority:
        fields["priority"] = {"name": priority}
    if labels:
        fields["labels"] = labels

    return client.post(f"{client.jira_base}/issue", json_data={"fields": fields})


async def acreate_issue(
    client: AsyncAtlassianClient,
    project_key: str,
    summary: str,
    issue_type: str = "Task",
    description: Optional[str] = None,
    assignee_id: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new Jira issue (async)."""
    fields: Dict[str, Any] = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"name": issue_type},
    }
    if description:
        fields["description"] = _text_to_adf(description)
    if assignee_id:
        fields["assignee"] = {"accountId": assignee_id}
    if priority:
        fields["priority"] = {"name": priority}
    if labels:
        fields["labels"] = labels

    return await client.post(f"{client.jira_base}/issue", json_data={"fields": fields})


# ---------------------------------------------------------------------------
# Update Issue
# ---------------------------------------------------------------------------

def update_issue(
    client: AtlassianClient,
    issue_key: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    assignee_id: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> None:
    """Update an existing Jira issue. Returns None (204)."""
    fields: Dict[str, Any] = {}
    if summary:
        fields["summary"] = summary
    if description:
        fields["description"] = _text_to_adf(description)
    if assignee_id:
        fields["assignee"] = {"accountId": assignee_id}
    if priority:
        fields["priority"] = {"name": priority}
    if labels is not None:
        fields["labels"] = labels

    client.put(f"{client.jira_base}/issue/{issue_key}", json_data={"fields": fields})


async def aupdate_issue(
    client: AsyncAtlassianClient,
    issue_key: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    assignee_id: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> None:
    """Update an existing Jira issue (async). Returns None (204)."""
    fields: Dict[str, Any] = {}
    if summary:
        fields["summary"] = summary
    if description:
        fields["description"] = _text_to_adf(description)
    if assignee_id:
        fields["assignee"] = {"accountId": assignee_id}
    if priority:
        fields["priority"] = {"name": priority}
    if labels is not None:
        fields["labels"] = labels

    await client.put(f"{client.jira_base}/issue/{issue_key}", json_data={"fields": fields})


# ---------------------------------------------------------------------------
# Add Issue Comment
# ---------------------------------------------------------------------------

def _build_comment_adf(body: str, author_label: str = "") -> Dict[str, Any]:
    """Build an ADF comment body, optionally prefixed with an author label."""
    adf = _text_to_adf(body)
    if author_label:
        prefix = {
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "text": f"\U0001f916 {author_label} (Bond AI):",
                    "marks": [{"type": "strong"}, {"type": "em"}],
                }
            ],
        }
        adf["content"] = [prefix] + adf["content"]
    return adf


def add_issue_comment(
    client: AtlassianClient,
    issue_key: str,
    body: str,
    author_label: str = "",
) -> Dict[str, Any]:
    """Add a comment to an issue. Supports @{accountId} mentions and optional author label."""
    return client.post(
        f"{client.jira_base}/issue/{issue_key}/comment",
        json_data={"body": _build_comment_adf(body, author_label)},
    )


async def aadd_issue_comment(
    client: AsyncAtlassianClient,
    issue_key: str,
    body: str,
    author_label: str = "",
) -> Dict[str, Any]:
    """Add a comment to an issue (async). Supports @{accountId} mentions and optional author label."""
    return await client.post(
        f"{client.jira_base}/issue/{issue_key}/comment",
        json_data={"body": _build_comment_adf(body, author_label)},
    )


# ---------------------------------------------------------------------------
# Transition Issue
# ---------------------------------------------------------------------------

def get_transitions(
    client: AtlassianClient,
    issue_key: str,
) -> List[Dict[str, Any]]:
    """Get available transitions for an issue."""
    data = client.get(f"{client.jira_base}/issue/{issue_key}/transitions")
    return data.get("transitions", [])


async def aget_transitions(
    client: AsyncAtlassianClient,
    issue_key: str,
) -> List[Dict[str, Any]]:
    """Get available transitions for an issue (async)."""
    data = await client.get(f"{client.jira_base}/issue/{issue_key}/transitions")
    return data.get("transitions", [])


def transition_issue(
    client: AtlassianClient,
    issue_key: str,
    transition_name: str,
) -> str:
    """Transition an issue by name (e.g., 'In Progress', 'Done').

    Returns the matched transition name on success.
    Raises AtlassianError with available transitions if name doesn't match.
    """
    from .atlassian_client import AtlassianError

    transitions = get_transitions(client, issue_key)
    match = _find_transition(transitions, transition_name)
    if not match:
        available = ", ".join(f"'{t['name']}'" for t in transitions)
        raise AtlassianError(
            400,
            "InvalidTransition",
            f"Transition '{transition_name}' not available for {issue_key}. "
            f"Available transitions: {available}",
        )

    client.post(
        f"{client.jira_base}/issue/{issue_key}/transitions",
        json_data={"transition": {"id": match["id"]}},
    )
    return match["name"]


async def atransition_issue(
    client: AsyncAtlassianClient,
    issue_key: str,
    transition_name: str,
) -> str:
    """Transition an issue by name (async).

    Returns the matched transition name on success.
    Raises AtlassianError with available transitions if name doesn't match.
    """
    from .atlassian_client import AtlassianError

    transitions = await aget_transitions(client, issue_key)
    match = _find_transition(transitions, transition_name)
    if not match:
        available = ", ".join(f"'{t['name']}'" for t in transitions)
        raise AtlassianError(
            400,
            "InvalidTransition",
            f"Transition '{transition_name}' not available for {issue_key}. "
            f"Available transitions: {available}",
        )

    await client.post(
        f"{client.jira_base}/issue/{issue_key}/transitions",
        json_data={"transition": {"id": match["id"]}},
    )
    return match["name"]


def _find_transition(
    transitions: List[Dict[str, Any]], name: str
) -> Optional[Dict[str, Any]]:
    """Find a transition by name (case-insensitive)."""
    lower = name.lower()
    for t in transitions:
        if t.get("name", "").lower() == lower:
            return t
    return None


# ---------------------------------------------------------------------------
# Get Project Versions
# ---------------------------------------------------------------------------

def get_project_versions(
    client: AtlassianClient,
    project_key: str,
    query: str = "",
    status: str = "",
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """Get versions for a Jira project, optionally filtered by name pattern and status."""
    params: Dict[str, Any] = {"maxResults": _cap(max_results)}
    if query:
        params["query"] = query
    if status:
        params["status"] = status
    data = client.get(
        f"{client.jira_base}/project/{project_key}/version",
        params=params,
    )
    return data.get("values", [])


async def aget_project_versions(
    client: AsyncAtlassianClient,
    project_key: str,
    query: str = "",
    status: str = "",
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """Get versions for a Jira project (async), optionally filtered by name pattern and status."""
    params: Dict[str, Any] = {"maxResults": _cap(max_results)}
    if query:
        params["query"] = query
    if status:
        params["status"] = status
    data = await client.get(
        f"{client.jira_base}/project/{project_key}/version",
        params=params,
    )
    return data.get("values", [])


# ---------------------------------------------------------------------------
# Create Version
# ---------------------------------------------------------------------------

def create_version(
    client: AtlassianClient,
    project_key: str,
    name: str,
    description: str = "",
    release_date: str = "",
    released: bool = False,
) -> Dict[str, Any]:
    """Create a new version for a Jira project."""
    payload: Dict[str, Any] = {
        "project": project_key,
        "name": name,
        "released": released,
    }
    if description:
        payload["description"] = description
    if release_date:
        payload["releaseDate"] = release_date
    return client.post(f"{client.jira_base}/version", json_data=payload)


async def acreate_version(
    client: AsyncAtlassianClient,
    project_key: str,
    name: str,
    description: str = "",
    release_date: str = "",
    released: bool = False,
) -> Dict[str, Any]:
    """Create a new version for a Jira project (async)."""
    payload: Dict[str, Any] = {
        "project": project_key,
        "name": name,
        "released": released,
    }
    if description:
        payload["description"] = description
    if release_date:
        payload["releaseDate"] = release_date
    return await client.post(f"{client.jira_base}/version", json_data=payload)


# ---------------------------------------------------------------------------
# Search Users
# ---------------------------------------------------------------------------

def search_users(
    client: AtlassianClient,
    query: str,
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """Search for Jira users by name or email."""
    data = client.get(
        f"{client.jira_base}/user/search",
        params={"query": query, "maxResults": _cap(max_results)},
    )
    return data if isinstance(data, list) else []


async def asearch_users(
    client: AsyncAtlassianClient,
    query: str,
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """Search for Jira users by name or email (async)."""
    data = await client.get(
        f"{client.jira_base}/user/search",
        params={"query": query, "maxResults": _cap(max_results)},
    )
    return data if isinstance(data, list) else []
