"""
Thin httpx wrapper for the Atlassian REST APIs.

Provides sync and async clients that handle authorization headers
and error parsing for both Jira v3 and Confluence v2 APIs.
"""

import httpx
from typing import Any, Dict, Optional

ATLASSIAN_API_BASE = "https://api.atlassian.com"

# Cap error message length to avoid huge payloads in exceptions
_MAX_ERROR_MESSAGE_LEN = 1000


class AtlassianError(Exception):
    """Raised when the Atlassian API returns an error response."""

    def __init__(self, status_code: int, error_code: str, message: str):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Atlassian API error {status_code} ({error_code}): {message}")


def _raise_for_atlassian_error(response: httpx.Response) -> None:
    """Raise AtlassianError if the response indicates failure."""
    if response.is_success:
        return

    error_code = "Unknown"
    message = ""

    try:
        body = response.json()

        # Jira error format: {"errorMessages": [...], "errors": {...}}
        error_messages = body.get("errorMessages", [])
        errors = body.get("errors", {})
        if error_messages or errors:
            parts = list(error_messages[:5])
            for field, msg in list(errors.items())[:5]:
                parts.append(f"{field}: {msg}")
            message = "; ".join(parts) if parts else ""

        # Confluence error format: {"code": ..., "message": "..."}
        if not message and "message" in body:
            message = body["message"]

        # Generic error message
        if not message:
            message = body.get("error", "")

        if not message:
            message = response.text[:_MAX_ERROR_MESSAGE_LEN] if response.text else response.reason_phrase
    except Exception:
        raw = response.text or response.reason_phrase or "Unknown error"
        message = raw[:_MAX_ERROR_MESSAGE_LEN]

    # Classify error codes
    if response.status_code == 401:
        error_code = "Unauthorized"
    elif response.status_code == 403:
        error_code = "Forbidden"
    elif response.status_code == 404:
        error_code = "NotFound"
    elif response.status_code == 429:
        error_code = "RateLimited"
        retry_after = response.headers.get("retry-after", "")
        if retry_after:
            message = f"{message} | Retry after {retry_after} seconds."
    elif response.status_code == 400:
        error_code = "BadRequest"
    elif response.status_code == 409:
        error_code = "Conflict"

    raise AtlassianError(response.status_code, error_code, message)


class AtlassianClient:
    """Synchronous Atlassian API client."""

    def __init__(self, access_token: str, cloud_id: str) -> None:
        self._cloud_id = cloud_id
        self._client = httpx.Client(
            base_url=ATLASSIAN_API_BASE,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    @property
    def jira_base(self) -> str:
        """Base URL path for Jira REST API v3."""
        return f"/ex/jira/{self._cloud_id}/rest/api/3"

    @property
    def confluence_base(self) -> str:
        """Base URL path for Confluence REST API v2."""
        return f"/ex/confluence/{self._cloud_id}/wiki/api/v2"

    @property
    def confluence_v1_base(self) -> str:
        """Base URL path for Confluence REST API v1 (used for CQL search)."""
        return f"/ex/confluence/{self._cloud_id}/wiki/rest/api"

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = self._client.get(path, params=params)
        _raise_for_atlassian_error(response)
        return response.json()

    def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        response = self._client.post(path, json=json_data)
        _raise_for_atlassian_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def put(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        response = self._client.put(path, json=json_data)
        _raise_for_atlassian_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AtlassianClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncAtlassianClient:
    """Asynchronous Atlassian API client."""

    def __init__(self, access_token: str, cloud_id: str) -> None:
        self._cloud_id = cloud_id
        self._client = httpx.AsyncClient(
            base_url=ATLASSIAN_API_BASE,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    @property
    def jira_base(self) -> str:
        """Base URL path for Jira REST API v3."""
        return f"/ex/jira/{self._cloud_id}/rest/api/3"

    @property
    def confluence_base(self) -> str:
        """Base URL path for Confluence REST API v2."""
        return f"/ex/confluence/{self._cloud_id}/wiki/api/v2"

    @property
    def confluence_v1_base(self) -> str:
        """Base URL path for Confluence REST API v1 (used for CQL search)."""
        return f"/ex/confluence/{self._cloud_id}/wiki/rest/api"

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = await self._client.get(path, params=params)
        _raise_for_atlassian_error(response)
        return response.json()

    async def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        response = await self._client.post(path, json=json_data)
        _raise_for_atlassian_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def put(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        response = await self._client.put(path, json=json_data)
        _raise_for_atlassian_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncAtlassianClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
