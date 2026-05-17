"""Tests for the Atlassian HTTP client."""

import httpx
import pytest
import respx

from atlassian.atlassian_client import (
    ATLASSIAN_API_BASE,
    AtlassianClient,
    AsyncAtlassianClient,
    AtlassianError,
    _raise_for_atlassian_error,
)

CLOUD_ID = "test-cloud-id"
TOKEN = "test-token"


class TestAtlassianError:
    """Test the AtlassianError exception."""

    def test_stores_status_and_code(self):
        err = AtlassianError(404, "NotFound", "Issue not found")
        assert err.status_code == 404
        assert err.error_code == "NotFound"
        assert "404" in str(err)
        assert "NotFound" in str(err)

    def test_message_format(self):
        err = AtlassianError(401, "Unauthorized", "Bad token")
        assert "Atlassian API error 401 (Unauthorized): Bad token" in str(err)


class TestRaiseForError:
    """Test the error parsing function."""

    def test_success_does_nothing(self):
        response = httpx.Response(200, json={"ok": True})
        _raise_for_atlassian_error(response)  # Should not raise

    def test_204_does_nothing(self):
        response = httpx.Response(204)
        _raise_for_atlassian_error(response)

    def test_401_raises_unauthorized(self):
        response = httpx.Response(401, json={"message": "Unauthorized"})
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert exc_info.value.status_code == 401
        assert exc_info.value.error_code == "Unauthorized"

    def test_404_jira_format(self):
        response = httpx.Response(
            404,
            json={
                "errorMessages": ["Issue does not exist."],
                "errors": {},
            },
        )
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert exc_info.value.status_code == 404
        assert exc_info.value.error_code == "NotFound"
        assert "Issue does not exist" in str(exc_info.value)

    def test_400_jira_validation_errors(self):
        response = httpx.Response(
            400,
            json={
                "errorMessages": [],
                "errors": {
                    "summary": "Summary is required",
                    "project": "Project not found",
                },
            },
        )
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert exc_info.value.error_code == "BadRequest"
        assert "Summary is required" in str(exc_info.value)
        assert "Project not found" in str(exc_info.value)

    def test_403_raises_forbidden(self):
        response = httpx.Response(403, json={"message": "Forbidden"})
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert exc_info.value.error_code == "Forbidden"

    def test_429_with_retry_after(self):
        response = httpx.Response(
            429,
            json={"message": "Rate limit exceeded"},
            headers={"retry-after": "30"},
        )
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert exc_info.value.error_code == "RateLimited"
        assert "30 seconds" in str(exc_info.value)

    def test_confluence_error_format(self):
        response = httpx.Response(
            400,
            json={"code": 400, "message": "Invalid space key"},
        )
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert "Invalid space key" in str(exc_info.value)

    def test_non_json_error(self):
        response = httpx.Response(500, text="Internal Server Error")
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert exc_info.value.status_code == 500

    def test_409_conflict(self):
        response = httpx.Response(409, json={"message": "Version conflict"})
        with pytest.raises(AtlassianError) as exc_info:
            _raise_for_atlassian_error(response)
        assert exc_info.value.error_code == "Conflict"


class TestAsyncClient:
    """Test the async HTTP client."""

    @respx.mock
    async def test_get(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.get(url).mock(return_value=httpx.Response(200, json={"ok": True}))

        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await client.get("/test")
        assert data == {"ok": True}

    @respx.mock
    async def test_post(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.post(url).mock(return_value=httpx.Response(201, json={"id": "123"}))

        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await client.post("/test", json_data={"name": "test"})
        assert data == {"id": "123"}

    @respx.mock
    async def test_post_204(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.post(url).mock(return_value=httpx.Response(204))

        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await client.post("/test", json_data={})
        assert data is None

    @respx.mock
    async def test_put(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.put(url).mock(return_value=httpx.Response(200, json={"updated": True}))

        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            data = await client.put("/test", json_data={"name": "updated"})
        assert data == {"updated": True}

    @respx.mock
    async def test_error_raises(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.get(url).mock(return_value=httpx.Response(404, json={"errorMessages": ["Not found"], "errors": {}}))

        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            with pytest.raises(AtlassianError) as exc_info:
                await client.get("/test")
        assert exc_info.value.status_code == 404

    def test_jira_base_property(self):
        client = AsyncAtlassianClient(TOKEN, CLOUD_ID)
        assert client.jira_base == f"/ex/jira/{CLOUD_ID}/rest/api/3"

    def test_confluence_base_property(self):
        client = AsyncAtlassianClient(TOKEN, CLOUD_ID)
        assert client.confluence_base == f"/ex/confluence/{CLOUD_ID}/wiki/api/v2"

    def test_confluence_v1_base_property(self):
        client = AsyncAtlassianClient(TOKEN, CLOUD_ID)
        assert client.confluence_v1_base == f"/ex/confluence/{CLOUD_ID}/wiki/rest/api"


# ---------------------------------------------------------------------------
# Sync client tests (M9 from review)
# ---------------------------------------------------------------------------

class TestSyncClient:
    """Test the synchronous HTTP client."""

    @respx.mock
    def test_get(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.get(url).mock(return_value=httpx.Response(200, json={"ok": True}))

        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            data = client.get("/test")
        assert data == {"ok": True}

    @respx.mock
    def test_post(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.post(url).mock(return_value=httpx.Response(201, json={"id": "1"}))

        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            data = client.post("/test", json_data={"name": "test"})
        assert data == {"id": "1"}

    @respx.mock
    def test_put_204(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.put(url).mock(return_value=httpx.Response(204))

        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            result = client.put("/test", json_data={"name": "updated"})
        assert result is None

    @respx.mock
    def test_error_raises(self):
        url = f"{ATLASSIAN_API_BASE}/test"
        respx.get(url).mock(return_value=httpx.Response(
            401, json={"errorMessages": ["Unauthorized"], "errors": {}}
        ))

        with AtlassianClient(TOKEN, CLOUD_ID) as client:
            with pytest.raises(AtlassianError) as exc_info:
                client.get("/test")
        assert exc_info.value.error_code == "Unauthorized"

    def test_jira_base_property(self):
        client = AtlassianClient(TOKEN, CLOUD_ID)
        assert client.jira_base == f"/ex/jira/{CLOUD_ID}/rest/api/3"
        client.close()

    def test_confluence_v1_base_property(self):
        client = AtlassianClient(TOKEN, CLOUD_ID)
        assert client.confluence_v1_base == f"/ex/confluence/{CLOUD_ID}/wiki/rest/api"
        client.close()
