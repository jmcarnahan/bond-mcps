"""
Thin httpx wrapper for Microsoft Graph API.

Provides sync and async clients that handle authorization headers
and base URL routing for the Graph v1.0 endpoint.
"""

import httpx
from typing import Any, Dict, Optional

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    """Raised when the Graph API returns an error response."""

    def __init__(self, status_code: int, error_code: str, message: str):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Graph API error {status_code} ({error_code}): {message}")


def _raise_for_graph_error(response: httpx.Response) -> None:
    """Raise GraphError if the response indicates failure."""
    if response.is_success:
        return
    try:
        body = response.json()
        err = body.get("error", {})
        code = err.get("code", "Unknown")
        message = err.get("message", response.text)
    except Exception:
        code = "Unknown"
        message = response.text or response.reason_phrase
    # Include WWW-Authenticate header for 401s — it contains the actual reason
    if response.status_code == 401:
        www_auth = response.headers.get("www-authenticate", "")
        if www_auth:
            message = f"{message} | WWW-Authenticate: {www_auth}"
    raise GraphError(response.status_code, code, message)


class GraphClient:
    """Synchronous Microsoft Graph API client."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.Client(
            base_url=GRAPH_BASE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self._client.get(path, params=params)
        _raise_for_graph_error(response)
        return response.json()

    def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code == 202 or not response.content:
            return None
        return response.json()

    def get_operation_status(self, url: str) -> Dict[str, Any]:
        """GET an async operation monitor URL. Treats 200 and 303 as success (both carry JSON)."""
        response = self._client.get(url)
        if response.status_code in (200, 303):
            return response.json()
        _raise_for_graph_error(response)
        raise GraphError(response.status_code, "UnexpectedStatus",
                         f"Unexpected status {response.status_code} from operation monitor")

    def post_with_location(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> str:
        """POST that expects a 202 Accepted with a Location header (async Graph operations like copy)."""
        response = self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code != 202:
            raise GraphError(
                response.status_code, "UnexpectedStatus",
                f"Expected 202 Accepted, got {response.status_code}"
            )
        location = response.headers.get("Location", "")
        if not location:
            raise GraphError(response.status_code, "NoLocation", "Expected Location header in 202 response")
        return location

    def put(self, path: str, content: bytes, content_type: str = "application/octet-stream") -> Dict[str, Any]:
        """PUT raw bytes to a path (used for file uploads)."""
        response = self._client.put(path, content=content, headers={"Content-Type": content_type})
        _raise_for_graph_error(response)
        return response.json()

    def patch(self, path: str, json_data: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH a resource with a JSON payload."""
        response = self._client.patch(path, json=json_data)
        _raise_for_graph_error(response)
        return response.json()

    def get_bytes(self, path: str, params: Optional[Dict[str, Any]] = None) -> bytes:
        """GET request returning raw bytes. Follows redirects (Graph /content returns 302)."""
        response = self._client.get(path, params=params, follow_redirects=True)
        _raise_for_graph_error(response)
        return response.content

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncGraphClient:
    """Asynchronous Microsoft Graph API client."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=GRAPH_BASE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = await self._client.get(path, params=params)
        _raise_for_graph_error(response)
        return response.json()

    async def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = await self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code == 202 or not response.content:
            return None
        return response.json()

    async def get_operation_status(self, url: str) -> Dict[str, Any]:
        """GET an async operation monitor URL. Treats 200 and 303 as success (both carry JSON)."""
        response = await self._client.get(url)
        if response.status_code in (200, 303):
            return response.json()
        _raise_for_graph_error(response)
        raise GraphError(response.status_code, "UnexpectedStatus",
                         f"Unexpected status {response.status_code} from operation monitor")

    async def post_with_location(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> str:
        """POST that expects a 202 Accepted with a Location header (async Graph operations like copy)."""
        response = await self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code != 202:
            raise GraphError(
                response.status_code, "UnexpectedStatus",
                f"Expected 202 Accepted, got {response.status_code}"
            )
        location = response.headers.get("Location", "")
        if not location:
            raise GraphError(response.status_code, "NoLocation", "Expected Location header in 202 response")
        return location

    async def put(self, path: str, content: bytes, content_type: str = "application/octet-stream") -> Dict[str, Any]:
        """PUT raw bytes to a path (used for file uploads)."""
        response = await self._client.put(path, content=content, headers={"Content-Type": content_type})
        _raise_for_graph_error(response)
        return response.json()

    async def patch(self, path: str, json_data: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH a resource with a JSON payload."""
        response = await self._client.patch(path, json=json_data)
        _raise_for_graph_error(response)
        return response.json()

    async def get_bytes(self, path: str, params: Optional[Dict[str, Any]] = None) -> bytes:
        """GET request returning raw bytes. Follows redirects (Graph /content returns 302)."""
        response = await self._client.get(path, params=params, follow_redirects=True)
        _raise_for_graph_error(response)
        return response.content

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncGraphClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
