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

UPLOAD_URL = "https://sn3302.up.1drv.com/up/session-abc"
FINAL_LOCATION = (
    "https://outlook.office.com/api/v2.0/Users('u1')/Messages('m1')/Attachments('AAMkAtt001%3D')"
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
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
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
                headers={
                    "WWW-Authenticate": 'Bearer realm="", authorization_uri="https://login.microsoftonline.com/common/oauth2/authorize"'
                },
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
            result = client.post(
                "/teams/t1/channels/c1/messages", json_data={"body": {"content": "Hi"}}
            )

        assert result == {"id": "msg-001", "body": {"content": "Hi"}}

    @respx.mock
    def test_get_operation_status_200(self):
        """200 response returns parsed JSON."""
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(
                200, json={"status": "inProgress", "percentageComplete": 50.0}
            )
        )
        with GraphClient("tok") as client:
            result = client.get_operation_status(monitor_url)

        assert result["status"] == "inProgress"

    @respx.mock
    def test_get_operation_status_303(self):
        """303 response (Graph copy completion signal) returns parsed JSON body."""
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(
                303,
                json={"status": "completed", "resourceId": "item-001", "percentageComplete": 100.0},
            )
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
            return_value=httpx.Response(
                500, json={"error": {"code": "InternalError", "message": "Server error"}}
            )
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
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
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
            return_value=httpx.Response(
                200, json={"status": "inProgress", "percentageComplete": 50.0}
            )
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.get_operation_status(monitor_url)

        assert result["status"] == "inProgress"

    @respx.mock
    async def test_async_get_operation_status_303(self):
        """303 response (Graph copy completion signal) returns parsed JSON body."""
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(
                303,
                json={"status": "completed", "resourceId": "item-001", "percentageComplete": 100.0},
            )
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.get_operation_status(monitor_url)

        assert result["status"] == "completed"
        assert result["resourceId"] == "item-001"

    @respx.mock
    async def test_async_get_operation_status_error_raises(self):
        monitor_url = "https://api.onedrive.com/v1.0/monitor/abc"
        respx.get(monitor_url).mock(
            return_value=httpx.Response(
                500, json={"error": {"code": "InternalError", "message": "Server error"}}
            )
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

    @respx.mock
    async def test_async_delete_returns_none_on_204(self):
        path = "/me/mailFolders/inbox/messageRules/rule-001"
        route = respx.delete(f"{GRAPH_BASE_URL}{path}").mock(return_value=httpx.Response(204))
        async with AsyncGraphClient("del-token") as client:
            result = await client.delete(path)

        assert result is None
        assert route.called
        assert route.calls[0].request.headers["authorization"] == "Bearer del-token"

    @respx.mock
    async def test_async_delete_error_raises(self):
        path = "/me/mailFolders/inbox/messageRules/bad-id"
        respx.delete(f"{GRAPH_BASE_URL}{path}").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "ErrorItemNotFound", "message": "Not found"}}
            )
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await client.delete(path)

        assert exc_info.value.status_code == 404


class TestExtraHeaders:
    """Per-request header merging on get/post (used by the Desktop JSON ops)."""

    @respx.mock
    def test_get_merges_extra_headers(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me/messages/id").mock(
            return_value=httpx.Response(200, json={"id": "id"})
        )
        with GraphClient("tok") as client:
            client.get("/me/messages/id", headers={"Prefer": 'outlook.body-content-type="text"'})

        req = route.calls[0].request
        assert req.headers["prefer"] == 'outlook.body-content-type="text"'
        # Client-level defaults survive the merge.
        assert req.headers["authorization"] == "Bearer tok"

    @respx.mock
    def test_get_without_headers_is_unchanged(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json={"id": "u"})
        )
        with GraphClient("tok") as client:
            client.get("/me")

        req = route.calls[0].request
        assert "prefer" not in req.headers
        assert req.headers["authorization"] == "Bearer tok"

    @respx.mock
    def test_post_merges_extra_headers(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/messages/id/createReply").mock(
            return_value=httpx.Response(201, json={"id": "draft"})
        )
        with GraphClient("tok") as client:
            client.post(
                "/me/messages/id/createReply",
                headers={"Prefer": 'outlook.timezone="UTC"'},
            )

        req = route.calls[0].request
        assert req.headers["prefer"] == 'outlook.timezone="UTC"'
        assert req.headers["authorization"] == "Bearer tok"

    @respx.mock
    def test_post_without_headers_is_unchanged(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            assert client.post("/me/sendMail", json_data={"a": 1}) is None

        assert "prefer" not in route.calls[0].request.headers

    @respx.mock
    async def test_async_get_merges_extra_headers(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me/messages/id").mock(
            return_value=httpx.Response(200, json={"id": "id"})
        )
        async with AsyncGraphClient("tok") as client:
            await client.get("/me/messages/id", headers={"Prefer": "outlook.body-content-type"})

        req = route.calls[0].request
        assert req.headers["prefer"] == "outlook.body-content-type"
        assert req.headers["authorization"] == "Bearer tok"

    @respx.mock
    async def test_async_post_merges_extra_headers(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/messages/id/createReply").mock(
            return_value=httpx.Response(201, json={"id": "draft"})
        )
        async with AsyncGraphClient("tok") as client:
            await client.post(
                "/me/messages/id/createReply", headers={"Prefer": 'outlook.timezone="UTC"'}
            )

        assert route.calls[0].request.headers["prefer"] == 'outlook.timezone="UTC"'

    @respx.mock
    async def test_async_get_without_headers_is_unchanged(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json={"id": "u"})
        )
        async with AsyncGraphClient("tok") as client:
            await client.get("/me")

        assert "prefer" not in route.calls[0].request.headers


class TestGetBytesWithType:
    """get_bytes_with_type returns the Content-Type header alongside the bytes."""

    @respx.mock
    def test_returns_bytes_and_content_type(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/f1/content").mock(
            return_value=httpx.Response(
                200, content=b"PNGDATA", headers={"Content-Type": "image/png"}
            )
        )
        with GraphClient("tok") as client:
            data, content_type = client.get_bytes_with_type("/me/drive/items/f1/content")

        assert data == b"PNGDATA"
        assert content_type == "image/png"

    @respx.mock
    def test_missing_content_type_is_empty_string(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/f1/content").mock(
            return_value=httpx.Response(200, content=b"raw")
        )
        with GraphClient("tok") as client:
            _, content_type = client.get_bytes_with_type("/me/drive/items/f1/content")

        assert content_type == ""

    @respx.mock
    def test_follows_redirect(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/f1/content").mock(
            return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/blob"})
        )
        respx.get("https://cdn.example.com/blob").mock(
            return_value=httpx.Response(
                200, content=b"redirected", headers={"Content-Type": "application/pdf"}
            )
        )
        with GraphClient("tok") as client:
            data, content_type = client.get_bytes_with_type("/me/drive/items/f1/content")

        assert data == b"redirected"
        assert content_type == "application/pdf"

    @respx.mock
    def test_error_raises(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/f1/content").mock(
            return_value=httpx.Response(404, json={"error": {"code": "ItemNotFound"}})
        )
        with pytest.raises(GraphError) as exc:
            with GraphClient("tok") as client:
                client.get_bytes_with_type("/me/drive/items/f1/content")

        assert exc.value.status_code == 404

    @respx.mock
    async def test_async_returns_bytes_and_content_type(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/f1/content").mock(
            return_value=httpx.Response(
                200, content=b"PNGDATA", headers={"Content-Type": "image/png"}
            )
        )
        async with AsyncGraphClient("tok") as client:
            data, content_type = await client.get_bytes_with_type("/me/drive/items/f1/content")

        assert (data, content_type) == (b"PNGDATA", "image/png")

    @respx.mock
    async def test_async_follows_redirect(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/f1/content").mock(
            return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/blob"})
        )
        respx.get("https://cdn.example.com/blob").mock(
            return_value=httpx.Response(
                200, content=b"redirected", headers={"Content-Type": "application/pdf"}
            )
        )
        async with AsyncGraphClient("tok") as client:
            data, content_type = await client.get_bytes_with_type("/me/drive/items/f1/content")

        assert (data, content_type) == (b"redirected", "application/pdf")


class TestSyncDelete:
    """The sync client gained delete() for parity with the async one."""

    @respx.mock
    def test_delete_returns_none_on_204(self):
        route = respx.delete(f"{GRAPH_BASE_URL}/me/messages/draft-1").mock(
            return_value=httpx.Response(204)
        )
        with GraphClient("tok") as client:
            assert client.delete("/me/messages/draft-1") is None

        assert route.called
        assert route.calls[0].request.headers["authorization"] == "Bearer tok"

    @respx.mock
    def test_delete_error_raises(self):
        respx.delete(f"{GRAPH_BASE_URL}/me/messages/draft-1").mock(
            return_value=httpx.Response(403, json={"error": {"code": "AccessDenied"}})
        )
        with pytest.raises(GraphError) as exc:
            with GraphClient("tok") as client:
                client.delete("/me/messages/draft-1")

        assert exc.value.status_code == 403


class TestPutRangeSync:
    """put_range talks to pre-authenticated upload-session URLs."""

    @respx.mock
    def test_sends_range_and_length_without_bearer(self):
        route = respx.put(UPLOAD_URL).mock(
            return_value=httpx.Response(202, json={"nextExpectedRanges": ["5-9"]})
        )
        with GraphClient("tok") as client:
            result = client.put_range(UPLOAD_URL, b"01234", 0, 10)

        req = route.calls[0].request
        assert req.headers["content-range"] == "bytes 0-4/10"
        assert req.headers["content-length"] == "5"
        assert req.headers["content-type"] == "application/octet-stream"
        assert "authorization" not in req.headers
        assert result.status_code == 202
        assert result.body == {"nextExpectedRanges": ["5-9"]}
        assert result.location == ""

    @respx.mock
    def test_final_201_carries_location(self):
        respx.put(UPLOAD_URL).mock(
            return_value=httpx.Response(201, headers={"Location": FINAL_LOCATION})
        )
        with GraphClient("tok") as client:
            result = client.put_range(UPLOAD_URL, b"56789", 5, 10)

        assert result.status_code == 201
        assert result.body is None
        assert result.location == FINAL_LOCATION

    @respx.mock
    def test_offset_range_is_computed_from_start(self):
        route = respx.put(UPLOAD_URL).mock(return_value=httpx.Response(200, json={"id": "item-1"}))
        with GraphClient("tok") as client:
            result = client.put_range(UPLOAD_URL, b"XY", 8, 10, content_type="image/png")

        assert route.calls[0].request.headers["content-range"] == "bytes 8-9/10"
        assert route.calls[0].request.headers["content-type"] == "image/png"
        assert result.body == {"id": "item-1"}

    @respx.mock
    def test_failure_raises_graph_error(self):
        respx.put(UPLOAD_URL).mock(
            return_value=httpx.Response(416, json={"error": {"code": "InvalidRange"}})
        )
        with pytest.raises(GraphError) as exc:
            with GraphClient("tok") as client:
                client.put_range(UPLOAD_URL, b"01234", 0, 10)

        assert exc.value.status_code == 416


class TestPutRangeAsync:
    """Async twin of TestPutRangeSync."""

    @respx.mock
    async def test_sends_range_without_bearer(self):
        route = respx.put(UPLOAD_URL).mock(
            return_value=httpx.Response(202, json={"nextExpectedRanges": ["5-9"]})
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.put_range(UPLOAD_URL, b"01234", 0, 10)

        req = route.calls[0].request
        assert req.headers["content-range"] == "bytes 0-4/10"
        assert req.headers["content-length"] == "5"
        assert "authorization" not in req.headers
        assert result.body == {"nextExpectedRanges": ["5-9"]}

    @respx.mock
    async def test_final_201_carries_location(self):
        respx.put(UPLOAD_URL).mock(
            return_value=httpx.Response(201, headers={"Location": FINAL_LOCATION})
        )
        async with AsyncGraphClient("tok") as client:
            result = await client.put_range(UPLOAD_URL, b"56789", 5, 10)

        assert (result.status_code, result.body, result.location) == (201, None, FINAL_LOCATION)

    @respx.mock
    async def test_failure_raises_graph_error(self):
        respx.put(UPLOAD_URL).mock(
            return_value=httpx.Response(500, json={"error": {"code": "ServiceError"}})
        )
        with pytest.raises(GraphError) as exc:
            async with AsyncGraphClient("tok") as client:
                await client.put_range(UPLOAD_URL, b"01234", 0, 10)

        assert exc.value.status_code == 500


class TestDeleteUrl:
    """delete_url cancels an upload session through the bare client."""

    @respx.mock
    def test_204_is_success_and_sends_no_bearer(self):
        route = respx.delete(UPLOAD_URL).mock(return_value=httpx.Response(204))
        with GraphClient("tok") as client:
            assert client.delete_url(UPLOAD_URL) is None

        assert "authorization" not in route.calls[0].request.headers

    @respx.mock
    def test_200_is_success(self):
        respx.delete(UPLOAD_URL).mock(return_value=httpx.Response(200, json={}))
        with GraphClient("tok") as client:
            assert client.delete_url(UPLOAD_URL) is None

    @respx.mock
    def test_error_raises(self):
        respx.delete(UPLOAD_URL).mock(
            return_value=httpx.Response(404, json={"error": {"code": "SessionNotFound"}})
        )
        with pytest.raises(GraphError) as exc:
            with GraphClient("tok") as client:
                client.delete_url(UPLOAD_URL)

        assert exc.value.status_code == 404

    @respx.mock
    def test_unexpected_success_status_raises(self):
        respx.delete(UPLOAD_URL).mock(return_value=httpx.Response(202))
        with pytest.raises(GraphError) as exc:
            with GraphClient("tok") as client:
                client.delete_url(UPLOAD_URL)

        assert exc.value.error_code == "UnexpectedStatus"

    @respx.mock
    async def test_async_204_is_success(self):
        route = respx.delete(UPLOAD_URL).mock(return_value=httpx.Response(204))
        async with AsyncGraphClient("tok") as client:
            assert await client.delete_url(UPLOAD_URL) is None

        assert "authorization" not in route.calls[0].request.headers

    @respx.mock
    async def test_async_error_raises(self):
        respx.delete(UPLOAD_URL).mock(
            return_value=httpx.Response(404, json={"error": {"code": "SessionNotFound"}})
        )
        with pytest.raises(GraphError) as exc:
            async with AsyncGraphClient("tok") as client:
                await client.delete_url(UPLOAD_URL)

        assert exc.value.status_code == 404
