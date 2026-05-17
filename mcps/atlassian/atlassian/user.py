"""
User operations — sync and async pairs.

Uses Jira /myself (classic scopes) with fallback to /me (granular scopes).
"""

from typing import Any, Dict

from .atlassian_client import AtlassianClient, AsyncAtlassianClient, AtlassianError


def get_myself(client: AtlassianClient) -> Dict[str, Any]:
    """Get the current authenticated user's info."""
    try:
        return client.get(f"{client.jira_base}/myself")
    except AtlassianError as e:
        if e.status_code in (401, 403):
            data = client.get("/me")
            return _normalize_me(data)
        raise


async def aget_myself(client: AsyncAtlassianClient) -> Dict[str, Any]:
    """Get the current authenticated user's info (async)."""
    try:
        return await client.get(f"{client.jira_base}/myself")
    except AtlassianError as e:
        if e.status_code in (401, 403):
            data = await client.get("/me")
            return _normalize_me(data)
        raise


def _normalize_me(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize /me response to match Jira /myself field names."""
    return {
        "accountId": data.get("account_id", ""),
        "displayName": data.get("name", ""),
        "emailAddress": data.get("email", ""),
        "active": data.get("account_status", "") == "active",
        "timeZone": data.get("locale", ""),
    }
