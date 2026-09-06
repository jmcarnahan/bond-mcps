"""
Thin httpx wrapper for Microsoft Graph API.

Provides sync and async clients that handle authorization headers
and base URL routing for the Graph v1.0 endpoint.
"""

from dataclasses import dataclass
from typing import Any

import httpx

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Upload-session fragments can be several MB, so the bare client gets longer
# read/write budgets than the 30 s the Graph JSON calls use.
_UPLOAD_TIMEOUT = httpx.Timeout(30.0, read=120.0, write=120.0)


@dataclass(frozen=True)
class RangeResult:
    """One upload-session fragment response.

    Graph answers a fragment PUT three different ways: an intermediate fragment
    returns JSON with ``nextExpectedRanges``; the final OneDrive fragment
    returns the driveItem JSON; the final Outlook attachment fragment returns
    201 with an EMPTY body and the attachment URL in ``Location``. Callers need
    all three, so all three are carried here.
    """

    status_code: int
    body: dict[str, Any] | None
    location: str


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


def _timeout_kwargs(timeout: float | None) -> dict[str, Any]:
    """Pass a per-call timeout to httpx only when one was asked for."""
    return {} if timeout is None else {"timeout": timeout}


def _range_headers(data: bytes, start: int, total: int, content_type: str) -> dict[str, str]:
    """Headers for one upload-session fragment PUT."""
    return {
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
        "Content-Range": f"bytes {start}-{start + len(data) - 1}/{total}",
    }


def _range_result(response: httpx.Response) -> RangeResult:
    """Wrap a successful fragment response, tolerating an empty or non-JSON body."""
    body: dict[str, Any] | None = None
    if response.content:
        try:
            parsed = response.json()
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
    return RangeResult(response.status_code, body, response.headers.get("Location", ""))


def _check_cancel_response(response: httpx.Response) -> None:
    """Accept 200/204 from an upload-session cancel; raise GraphError otherwise."""
    if response.status_code in (200, 204):
        return
    _raise_for_graph_error(response)
    raise GraphError(
        response.status_code,
        "UnexpectedStatus",
        f"Unexpected status {response.status_code} cancelling the upload session",
    )


class GraphClient:
    """Synchronous Microsoft Graph API client."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.Client(
            base_url=GRAPH_BASE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        # Upload-session URLs are pre-authenticated: OneDrive answers 401 when a
        # bearer header is present, so those requests need a client that carries
        # no default headers and no base URL.
        self._bare_client = httpx.Client(timeout=_UPLOAD_TIMEOUT)

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._client.get(path, params=params, headers=headers)
        _raise_for_graph_error(response)
        return response.json()

    def post(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        response = self._client.post(path, json=json_data, headers=headers)
        _raise_for_graph_error(response)
        if response.status_code == 202 or not response.content:
            return None
        return response.json()

    def get_operation_status(self, url: str) -> dict[str, Any]:
        """GET an async operation monitor URL. Treats 200 and 303 as success (both carry JSON)."""
        response = self._client.get(url)
        if response.status_code in (200, 303):
            return response.json()
        _raise_for_graph_error(response)
        raise GraphError(
            response.status_code,
            "UnexpectedStatus",
            f"Unexpected status {response.status_code} from operation monitor",
        )

    def post_with_location(self, path: str, json_data: dict[str, Any] | None = None) -> str:
        """POST that expects a 202 Accepted with a Location header (async Graph operations like copy)."""
        response = self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code != 202:
            raise GraphError(
                response.status_code,
                "UnexpectedStatus",
                f"Expected 202 Accepted, got {response.status_code}",
            )
        location = response.headers.get("Location", "")
        if not location:
            raise GraphError(
                response.status_code, "NoLocation", "Expected Location header in 202 response"
            )
        return location

    def put(
        self, path: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        """PUT raw bytes to a path (used for file uploads)."""
        response = self._client.put(path, content=content, headers={"Content-Type": content_type})
        _raise_for_graph_error(response)
        return response.json()

    def patch(self, path: str, json_data: dict[str, Any]) -> dict[str, Any]:
        """PATCH a resource with a JSON payload."""
        response = self._client.patch(path, json=json_data)
        _raise_for_graph_error(response)
        return response.json()

    def get_bytes(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """GET request returning raw bytes. Follows redirects (Graph /content returns 302)."""
        response = self._client.get(
            path, params=params, follow_redirects=True, **_timeout_kwargs(timeout)
        )
        _raise_for_graph_error(response)
        return response.content

    def get_bytes_with_type(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, str]:
        """GET raw bytes plus the response Content-Type ("" when absent).

        Attachment and thumbnail endpoints never repeat the MIME type in a JSON
        body, so the header is the only place the caller can learn it.
        """
        response = self._client.get(
            path, params=params, follow_redirects=True, **_timeout_kwargs(timeout)
        )
        _raise_for_graph_error(response)
        return response.content, response.headers.get("Content-Type", "")

    def delete(self, path: str) -> None:
        """DELETE a resource (Graph replies 204 No Content with an empty body)."""
        response = self._client.delete(path)
        _raise_for_graph_error(response)

    def put_range(
        self,
        upload_url: str,
        data: bytes,
        start: int,
        total: int,
        content_type: str = "application/octet-stream",
    ) -> RangeResult:
        """PUT one fragment to a pre-authenticated upload-session URL.

        Sent through the bare client so no Authorization header goes out; both
        Outlook and OneDrive session URLs already carry their own credential.
        """
        response = self._bare_client.put(
            upload_url,
            content=data,
            headers=_range_headers(data, start, total, content_type),
        )
        _raise_for_graph_error(response)
        return _range_result(response)

    def delete_url(self, url: str) -> None:
        """DELETE an absolute pre-authenticated URL (cancels an upload session)."""
        response = self._bare_client.delete(url)
        _check_cancel_response(response)

    def close(self) -> None:
        self._client.close()
        self._bare_client.close()

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
        # See GraphClient.__init__ — upload-session URLs must go out unsigned.
        self._bare_client = httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT)

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.get(path, params=params, headers=headers)
        _raise_for_graph_error(response)
        return response.json()

    async def post(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        response = await self._client.post(path, json=json_data, headers=headers)
        _raise_for_graph_error(response)
        if response.status_code == 202 or not response.content:
            return None
        return response.json()

    async def get_operation_status(self, url: str) -> dict[str, Any]:
        """GET an async operation monitor URL. Treats 200 and 303 as success (both carry JSON)."""
        response = await self._client.get(url)
        if response.status_code in (200, 303):
            return response.json()
        _raise_for_graph_error(response)
        raise GraphError(
            response.status_code,
            "UnexpectedStatus",
            f"Unexpected status {response.status_code} from operation monitor",
        )

    async def post_with_location(self, path: str, json_data: dict[str, Any] | None = None) -> str:
        """POST that expects a 202 Accepted with a Location header (async Graph operations like copy)."""
        response = await self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code != 202:
            raise GraphError(
                response.status_code,
                "UnexpectedStatus",
                f"Expected 202 Accepted, got {response.status_code}",
            )
        location = response.headers.get("Location", "")
        if not location:
            raise GraphError(
                response.status_code, "NoLocation", "Expected Location header in 202 response"
            )
        return location

    async def put(
        self, path: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        """PUT raw bytes to a path (used for file uploads)."""
        response = await self._client.put(
            path, content=content, headers={"Content-Type": content_type}
        )
        _raise_for_graph_error(response)
        return response.json()

    async def patch(
        self,
        path: str,
        json_data: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """PATCH a resource with a JSON payload."""
        response = await self._client.patch(path, json=json_data, headers=headers)
        _raise_for_graph_error(response)
        return response.json()

    async def get_bytes(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """GET request returning raw bytes. Follows redirects (Graph /content returns 302)."""
        response = await self._client.get(
            path, params=params, follow_redirects=True, **_timeout_kwargs(timeout)
        )
        _raise_for_graph_error(response)
        return response.content

    async def get_bytes_with_type(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, str]:
        """GET raw bytes plus the response Content-Type ("" when absent) (async)."""
        response = await self._client.get(
            path, params=params, follow_redirects=True, **_timeout_kwargs(timeout)
        )
        _raise_for_graph_error(response)
        return response.content, response.headers.get("Content-Type", "")

    async def delete(self, path: str) -> None:
        """DELETE a resource (Graph replies 204 No Content with an empty body)."""
        response = await self._client.delete(path)
        _raise_for_graph_error(response)

    async def put_range(
        self,
        upload_url: str,
        data: bytes,
        start: int,
        total: int,
        content_type: str = "application/octet-stream",
    ) -> RangeResult:
        """PUT one fragment to a pre-authenticated upload-session URL (async).

        See :meth:`GraphClient.put_range` for why the bearer header is omitted.
        """
        response = await self._bare_client.put(
            upload_url,
            content=data,
            headers=_range_headers(data, start, total, content_type),
        )
        _raise_for_graph_error(response)
        return _range_result(response)

    async def delete_url(self, url: str) -> None:
        """DELETE an absolute pre-authenticated URL (cancels an upload session) (async)."""
        response = await self._bare_client.delete(url)
        _check_cancel_response(response)

    async def close(self) -> None:
        await self._client.aclose()
        await self._bare_client.aclose()

    async def __aenter__(self) -> "AsyncGraphClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
