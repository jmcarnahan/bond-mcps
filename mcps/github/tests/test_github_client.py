"""Tests for GitHubClient and AsyncGitHubClient."""

import httpx
import pytest
import respx

from github.github_client import (
    GITHUB_API_BASE_URL,
    AsyncGitHubClient,
    GitHubClient,
    GitHubError,
)


class TestGitHubClient:
    """Synchronous GitHubClient tests."""

    @respx.mock
    def test_get_sends_auth_and_version_headers(self):
        route = respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(200, json={"login": "octocat"})
        )
        with GitHubClient("ghp_test123") as client:
            result = client.get("/user")

        assert result == {"login": "octocat"}
        assert route.called
        req = route.calls[0].request
        assert req.headers["authorization"] == "Bearer ghp_test123"
        assert req.headers["x-github-api-version"] == "2022-11-28"

    @respx.mock
    def test_get_with_params(self):
        route = respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(200, json=[])
        )
        with GitHubClient("tok") as client:
            result = client.get("/user/repos", params={"type": "all", "per_page": 5})

        assert result == []
        assert "per_page=5" in str(route.calls[0].request.url)

    @respx.mock
    def test_post_sends_json(self):
        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(201, json={"number": 1})
        )
        with GitHubClient("tok") as client:
            result = client.post("/repos/o/r/issues", json_data={"title": "Test"})

        assert result == {"number": 1}
        assert route.called

    @respx.mock
    def test_post_returns_none_on_204(self):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/1/labels").mock(
            return_value=httpx.Response(204)
        )
        with GitHubClient("tok") as client:
            result = client.post("/repos/o/r/issues/1/labels")

        assert result is None

    @respx.mock
    def test_patch_sends_json(self):
        route = respx.patch(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/1").mock(
            return_value=httpx.Response(200, json={"number": 1, "state": "closed"})
        )
        with GitHubClient("tok") as client:
            result = client.patch("/repos/o/r/issues/1", json_data={"state": "closed"})

        assert result["state"] == "closed"
        assert route.called

    @respx.mock
    def test_put_sends_json(self):
        route = respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/1/merge").mock(
            return_value=httpx.Response(200, json={"merged": True})
        )
        with GitHubClient("tok") as client:
            result = client.put("/repos/o/r/pulls/1/merge", json_data={"merge_method": "squash"})

        assert result["merged"] is True
        assert route.called

    @respx.mock
    def test_delete_succeeds(self):
        route = respx.delete(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/1").mock(
            return_value=httpx.Response(204)
        )
        with GitHubClient("tok") as client:
            client.delete("/repos/o/r/issues/1")

        assert route.called

    @respx.mock
    def test_error_raises_github_error(self):
        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r").mock(
            return_value=httpx.Response(
                404,
                json={"message": "Not Found", "documentation_url": "https://docs.github.com/rest"},
            )
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.get("/repos/o/r")

        assert exc_info.value.status_code == 404

    @respx.mock
    def test_error_with_non_json_body(self):
        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.get("/user")

        assert exc_info.value.status_code == 500

    @respx.mock
    def test_rate_limit_error_includes_reset_info(self):
        respx.get(f"{GITHUB_API_BASE_URL}/user/repos").mock(
            return_value=httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
            )
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.get("/user/repos")

        assert exc_info.value.status_code == 403
        assert "Rate limit exceeded" in str(exc_info.value)
        assert "1700000000" in str(exc_info.value)

    @respx.mock
    def test_validation_error_includes_details(self):
        respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "title is missing"}],
                },
            )
        )
        with GitHubClient("tok") as client:
            with pytest.raises(GitHubError) as exc_info:
                client.post("/repos/o/r/issues", json_data={})

        assert exc_info.value.status_code == 422
        assert "title is missing" in str(exc_info.value)


class TestAsyncGitHubClient:
    """Async GitHubClient tests."""

    @respx.mock
    async def test_async_get_sends_auth_header(self):
        route = respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(200, json={"login": "octocat"})
        )
        async with AsyncGitHubClient("ghp_async") as client:
            result = await client.get("/user")

        assert result == {"login": "octocat"}
        req = route.calls[0].request
        assert req.headers["authorization"] == "Bearer ghp_async"

    @respx.mock
    async def test_async_post(self):
        route = respx.post(f"{GITHUB_API_BASE_URL}/repos/o/r/issues").mock(
            return_value=httpx.Response(201, json={"number": 1})
        )
        async with AsyncGitHubClient("tok") as client:
            result = await client.post("/repos/o/r/issues", json_data={"title": "Test"})

        assert result == {"number": 1}
        assert route.called

    @respx.mock
    async def test_async_patch(self):
        route = respx.patch(f"{GITHUB_API_BASE_URL}/repos/o/r/issues/1").mock(
            return_value=httpx.Response(200, json={"state": "closed"})
        )
        async with AsyncGitHubClient("tok") as client:
            result = await client.patch("/repos/o/r/issues/1", json_data={"state": "closed"})

        assert result["state"] == "closed"

    @respx.mock
    async def test_async_put(self):
        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/pulls/1/merge").mock(
            return_value=httpx.Response(200, json={"merged": True})
        )
        async with AsyncGitHubClient("tok") as client:
            result = await client.put("/repos/o/r/pulls/1/merge", json_data={})

        assert result["merged"] is True

    @respx.mock
    async def test_async_error(self):
        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(
                401,
                json={"message": "Bad credentials"},
            )
        )
        async with AsyncGitHubClient("bad") as client:
            with pytest.raises(GitHubError) as exc_info:
                await client.get("/user")

        assert exc_info.value.status_code == 401
