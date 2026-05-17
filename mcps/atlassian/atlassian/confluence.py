"""
Confluence operations — sync and async pairs.

Uses Confluence REST API v2 via the Atlassian cloud gateway.
"""

from typing import Any, Dict, List, Optional

from .atlassian_client import AtlassianClient, AsyncAtlassianClient


def _cap(value: int, maximum: int = 25) -> int:
    """Clamp a pagination value between 1 and maximum."""
    return min(max(value, 1), maximum)


# ---------------------------------------------------------------------------
# List Spaces
# ---------------------------------------------------------------------------

def list_spaces(
    client: AtlassianClient,
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """List accessible Confluence spaces."""
    data = client.get(
        f"{client.confluence_base}/spaces",
        params={"limit": _cap(max_results), "sort": "name"},
    )
    return data.get("results", [])


async def alist_spaces(
    client: AsyncAtlassianClient,
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """List accessible Confluence spaces (async)."""
    data = await client.get(
        f"{client.confluence_base}/spaces",
        params={"limit": _cap(max_results), "sort": "name"},
    )
    return data.get("results", [])


# ---------------------------------------------------------------------------
# Search Content
# ---------------------------------------------------------------------------

def search_content(
    client: AtlassianClient,
    query: str,
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """Search Confluence pages and blogs using CQL.

    Uses v1 content/search (still active) because v2 /search requires an
    unavailable scope. The v1 endpoint works with granular Confluence scopes.
    """
    data = client.get(
        f"{client.confluence_v1_base}/content/search",
        params={"cql": query, "limit": _cap(max_results)},
    )
    return data.get("results", [])


async def asearch_content(
    client: AsyncAtlassianClient,
    query: str,
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """Search Confluence pages and blogs using CQL (async)."""
    data = await client.get(
        f"{client.confluence_v1_base}/content/search",
        params={"cql": query, "limit": _cap(max_results)},
    )
    return data.get("results", [])


# ---------------------------------------------------------------------------
# Get Page
# ---------------------------------------------------------------------------

def get_page(
    client: AtlassianClient,
    page_id: str,
) -> Dict[str, Any]:
    """Get a Confluence page with body content."""
    return client.get(
        f"{client.confluence_base}/pages/{page_id}",
        params={"body-format": "storage"},
    )


async def aget_page(
    client: AsyncAtlassianClient,
    page_id: str,
) -> Dict[str, Any]:
    """Get a Confluence page with body content (async)."""
    return await client.get(
        f"{client.confluence_base}/pages/{page_id}",
        params={"body-format": "storage"},
    )


# ---------------------------------------------------------------------------
# Create Page
# ---------------------------------------------------------------------------

def create_page(
    client: AtlassianClient,
    space_id: str,
    title: str,
    body: str,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new Confluence page."""
    payload: Dict[str, Any] = {
        "spaceId": space_id,
        "status": "current",
        "title": title,
        "body": {
            "representation": "storage",
            "value": body,
        },
    }
    if parent_id:
        payload["parentId"] = parent_id

    return client.post(f"{client.confluence_base}/pages", json_data=payload)


async def acreate_page(
    client: AsyncAtlassianClient,
    space_id: str,
    title: str,
    body: str,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new Confluence page (async)."""
    payload: Dict[str, Any] = {
        "spaceId": space_id,
        "status": "current",
        "title": title,
        "body": {
            "representation": "storage",
            "value": body,
        },
    }
    if parent_id:
        payload["parentId"] = parent_id

    return await client.post(f"{client.confluence_base}/pages", json_data=payload)


# ---------------------------------------------------------------------------
# Update Page
# ---------------------------------------------------------------------------

def update_page(
    client: AtlassianClient,
    page_id: str,
    title: str,
    body: str,
    version_number: int,
) -> Dict[str, Any]:
    """Update an existing Confluence page."""
    payload = {
        "id": page_id,
        "status": "current",
        "title": title,
        "body": {
            "representation": "storage",
            "value": body,
        },
        "version": {
            "number": version_number,
        },
    }
    return client.put(f"{client.confluence_base}/pages/{page_id}", json_data=payload)


async def aupdate_page(
    client: AsyncAtlassianClient,
    page_id: str,
    title: str,
    body: str,
    version_number: int,
) -> Dict[str, Any]:
    """Update an existing Confluence page (async)."""
    payload = {
        "id": page_id,
        "status": "current",
        "title": title,
        "body": {
            "representation": "storage",
            "value": body,
        },
        "version": {
            "number": version_number,
        },
    }
    return await client.put(f"{client.confluence_base}/pages/{page_id}", json_data=payload)
