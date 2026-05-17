"""Tests for GraphClient and AsyncGraphClient."""

import httpx
import pytest
import respx

from ms_graph.graph_client import (
    GRAPH_BASE_URL,
    AsyncGraphClient,
    GraphClient,
    GraphError,
)


class TestGraphClient:
    """Synchronous GraphClient tests."""

    @respx.mock
    def test_get_sends_auth_header(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json={"displayName": "Test"})
        )
        with GraphClient("test-token-123") as client:
            result = client.get("/me")

        assert result == {"displayName": "Test"}
        assert route.called
        req = route.calls[0].request
        assert req.headers["authorization"] == "Bearer test-token-123"

    @respx.mock
    def test_get_with_params(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with GraphClient("tok") as client:
            result = client.get("/me/messages", params={"$top": 5})

        assert result == {"value": []}
        assert "top=5" in str(route.calls[0].request.url)

    @respx.mock
    def test_post_sends_json(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(
            return_value=httpx.Response(202)
        )
        with GraphClient("tok") as client:
            result = client.post("/me/sendMail", json_data={"message": {}})

        assert result is None  # 202 returns None
        assert route.called

    @respx.mock
    def test_error_raises_graph_error(self):
        respx.get(f"{GRAPH_BASE_URL}/me/messages/bad-id").mock(
            return_value=httpx.Response(
                404,
                json={
                    "error": {
                        "code": "ResourceNotFound",
                        "message": "Not found",
                    }
                },
            )
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                client.get("/me/messages/bad-id")

        assert exc_info.value.status_code == 404
        assert exc_info.value.error_code == "ResourceNotFound"

    @respx.mock
    def test_error_with_non_json_body(self):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                client.get("/me")

        assert exc_info.value.status_code == 500

    @respx.mock
    def test_401_includes_www_authenticate_header(self):
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"code": "InvalidAuthenticationToken", "message": "Token expired"}},
                headers={"WWW-Authenticate": 'Bearer realm="", authorization_uri="https://login.microsoftonline.com/common/oauth2/authorize"'},
            )
        )
        with GraphClient("expired-tok") as client:
            with pytest.raises(GraphError) as exc_info:
                client.get("/me/messages")

        assert exc_info.value.status_code == 401
        assert "WWW-Authenticate" in str(exc_info.value)
        assert "authorization_uri" in str(exc_info.value)

    @respx.mock
    def test_post_returns_json_on_success(self):
        respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(201, json={"id": "msg-001", "body": {"content": "Hi"}})
        )
        with GraphClient("tok") as client:
            result = client.post("/teams/t1/channels/c1/messages", json_data={"body": {"content": "Hi"}})

        assert result == {"id": "msg-001", "body": {"content": "Hi"}}

    @respx.mock
    def test_get_operation_status_200(self):
        """200 response returns parsed JSON."""
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(200, json={"status": "inProgress", "percentageComplete": 50.0})
        )
        with GraphClient("tok") as client:
            result = client.get_operation_status(monitor_url)

        assert result["status"] == "inProgress"

    @respx.mock
    def test_get_operation_status_303(self):
        """303 response (Graph copy completion signal) returns parsed JSON body."""
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(303, json={"status": "completed", "resourceId": "item-001", "percentageComplete": 100.0})
        )
        with GraphClient("tok") as client:
            result = client.get_operation_status(monitor_url)

        assert result["status"] == "completed"
        assert result["resourceId"] == "item-001"

    @respx.mock
    def test_get_operation_status_error_raises(self):
        """Non-2xx/3xx responses raise GraphError."""
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(500, json={"error": {"code": "InternalError", "message": "Server error"}})
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                client.get_operation_status(monitor_url)

        assert exc_info.value.status_code == 500

    @respx.mock
    def test_get_bytes_returns_raw_content(self):
        content = b"hello,world\n1,2\n"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/abc/content").mock(
            return_value=httpx.Response(200, content=content)
        )
        with GraphClient("tok") as client:
            result = client.get_bytes("/me/drive/items/abc/content")

        assert result == content

    @respx.mock
    def test_get_bytes_error_raises(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/bad/content").mock(
            return_value=httpx.Response(
                404,
                json={"error": {"code": "itemNotFound", "message": "Item not found"}},
            )
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                client.get_bytes("/me/drive/items/bad/content")

        assert exc_info.value.status_code == 404


class TestAsyncGraphClient:
    """Async GraphClient tests."""

    @respx.mock
    async def test_async_get_sends_auth_header(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json={"displayName": "Test"})
        )
        async with AsyncGraphClient("async-token") as client:
            result = await client.get("/me")

        assert result == {"displayName": "Test"}
        req = route.calls[0].request
        assert req.headers["authorization"] == "Bearer async-token"

    @respx.mock
    async def test_async_post(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(
            return_value=httpx.Response(202)
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.post("/me/sendMail", json_data={"message": {}})

        assert result is None
        assert route.called

    @respx.mock
    async def test_async_error(self):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(
                401,
                json={
                    "error": {
                        "code": "InvalidAuthenticationToken",
                        "message": "Token expired",
                    }
                },
            )
        )
        async with AsyncGraphClient("expired") as client:
            with pytest.raises(GraphError) as exc_info:
                await client.get("/me")

        assert exc_info.value.status_code == 401

    @respx.mock
    async def test_async_get_operation_status_200(self):
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(200, json={"status": "inProgress", "percentageComplete": 50.0})
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.get_operation_status(monitor_url)

        assert result["status"] == "inProgress"

    @respx.mock
    async def test_async_get_operation_status_303(self):
        """303 response (Graph copy completion signal) returns parsed JSON body."""
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(303, json={"status": "completed", "resourceId": "item-001", "percentageComplete": 100.0})
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.get_operation_status(monitor_url)

        assert result["status"] == "completed"
        assert result["resourceId"] == "item-001"

    @respx.mock
    async def test_async_get_operation_status_error_raises(self):
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(500, json={"error": {"code": "InternalError", "message": "Server error"}})
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await client.get_operation_status(monitor_url)

        assert exc_info.value.status_code == 500

    @respx.mock
    async def test_async_get_bytes_returns_raw_content(self):
        content = b"\x89PNG\r\n\x1a\n"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/img/content").mock(
            return_value=httpx.Response(200, content=content)
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.get_bytes("/me/drive/items/img/content")

        assert result == content

    @respx.mock
    async def test_async_get_bytes_error_raises(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/bad/content").mock(
            return_value=httpx.Response(
                404,
                json={"error": {"code": "itemNotFound", "message": "Not found"}},
            )
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await client.get_bytes("/me/drive/items/bad/content")

        assert exc_info.value.status_code == 404
