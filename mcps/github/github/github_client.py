"""
Thin httpx wrapper for the GitHub REST API.

Provides sync and async clients that handle authorization headers,
API versioning, and error parsing for the GitHub v3 REST API.
"""

import httpx
from typing import Any, Dict, Optional

GITHUB_API_BASE_URL = "https://api.github.com"

# Standard headers for GitHub API requests
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Cap error message length to avoid huge payloads in exceptions
_MAX_ERROR_MESSAGE_LEN = 1000


class GitHubError(Exception):
    """Raised when the GitHub API returns an error response."""

    def __init__(self, status_code: int, error_code: str, message: str):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"GitHub API error {status_code} ({error_code}): {message}")


def _raise_for_github_error(response: httpx.Response) -> None:
    """Raise GitHubError if the response indicates failure."""
    if response.is_success:
        return

    error_code = "Unknown"
    try:
        body = response.json()
        message = body.get("message", "")
        error_code = body.get("message", "Unknown")  # GitHub uses message as the error identifier
        errors = body.get("errors", [])
        if errors:
            details = "; ".join(
                e.get("message", str(e)) for e in errors[:5]  # cap at 5 errors
            )
            message = f"{message} ({details})"
        if not message:
            message = response.text[:_MAX_ERROR_MESSAGE_LEN] if response.text else response.reason_phrase
    except Exception:
        raw = response.text or response.reason_phrase or "Unknown error"
        message = raw[:_MAX_ERROR_MESSAGE_LEN]

    # Include rate limit info for 403s
    if response.status_code == 403:
        remaining = response.headers.get("x-ratelimit-remaining", "")
        if remaining == "0":
            reset = response.headers.get("x-ratelimit-reset", "?")
            error_code = "RateLimitExceeded"
            message = f"{message} | Rate limit exceeded. Resets at epoch {reset}."

    # Classify common error codes
    if response.status_code == 401:
        error_code = "BadCredentials"
    elif response.status_code == 404:
        error_code = "NotFound"
    elif response.status_code == 422:
        error_code = "ValidationFailed"

    raise GitHubError(response.status_code, error_code, message)


class GitHubClient:
    """Synchronous GitHub API client."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.Client(
            base_url=GITHUB_API_BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                **_GITHUB_HEADERS,
            },
            timeout=30.0,
        )

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self._client.get(path, params=params)
        _raise_for_github_error(response)
        return response.json()

    def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = self._client.post(path, json=json_data)
        _raise_for_github_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def patch(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self._client.patch(path, json=json_data)
        _raise_for_github_error(response)
        return response.json()

    def put(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = self._client.put(path, json=json_data)
        _raise_for_github_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def delete(self, path: str) -> None:
        response = self._client.delete(path)
        _raise_for_github_error(response)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncGitHubClient:
    """Asynchronous GitHub API client."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                **_GITHUB_HEADERS,
            },
            timeout=30.0,
        )

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = await self._client.get(path, params=params)
        _raise_for_github_error(response)
        return response.json()

    async def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = await self._client.post(path, json=json_data)
        _raise_for_github_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def patch(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = await self._client.patch(path, json=json_data)
        _raise_for_github_error(response)
        return response.json()

    async def put(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = await self._client.put(path, json=json_data)
        _raise_for_github_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def delete(self, path: str) -> None:
        response = await self._client.delete(path)
        _raise_for_github_error(response)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncGitHubClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
