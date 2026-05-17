"""Tests for proxy_server.py -- OAuth callback proxy HTTP server."""

import http.client
import json
import threading
import time
from http.server import HTTPServer

import pytest

from auth.proxy_server import (
    AuthProxyHandler, _pending, _lock, PendingAuth,
)


@pytest.fixture(autouse=True)
def _clear_state():
    """Clear pending state before each test."""
    with _lock:
        _pending.clear()
    yield
    with _lock:
        _pending.clear()


@pytest.fixture()
def server():
    """Start proxy server in a thread, return (server, port)."""
    srv = HTTPServer(("127.0.0.1", 0), AuthProxyHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, port
    srv.shutdown()
    srv.server_close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


def _post_json(port, path, data):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(data).encode()
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp_body = resp.read().decode()
    conn.close()
    return resp.status, resp_body


class TestHealthEndpoint:
    def test_health_returns_ok(self, server):
        _, port = server
        status, body = _get(port, "/health")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}


class TestRegisterEndpoint:
    def test_register_success(self, server):
        _, port = server
        status, body = _post_json(port, "/auth/register",
                                  {"state": "abc", "provider": "microsoft"})
        assert status == 200
        assert json.loads(body)["registered"] is True
        assert "abc" in _pending

    def test_register_duplicate_state(self, server):
        _, port = server
        _post_json(port, "/auth/register", {"state": "dup", "provider": "microsoft"})
        status, body = _post_json(port, "/auth/register",
                                  {"state": "dup", "provider": "microsoft"})
        assert status == 409

    def test_register_missing_fields(self, server):
        _, port = server
        status, _ = _post_json(port, "/auth/register", {"state": "x"})
        assert status == 400

    def test_register_invalid_json(self, server):
        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/auth/register", body=b"not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()


class TestCallbackEndpoint:
    def test_callback_success(self, server):
        _, port = server
        with _lock:
            _pending["state123"] = PendingAuth(provider="microsoft", created_at=time.time())

        status, body = _get(port,
            "/connections/microsoft/callback?code=authcode&state=state123")
        assert status == 200
        assert "successful" in body.lower()
        with _lock:
            assert _pending["state123"].result["code"] == "authcode"

    def test_callback_unknown_state(self, server):
        _, port = server
        status, body = _get(port,
            "/connections/microsoft/callback?code=x&state=unknown")
        assert status == 400
        assert "failed" in body.lower()

    def test_callback_provider_mismatch(self, server):
        _, port = server
        with _lock:
            _pending["s1"] = PendingAuth(provider="microsoft", created_at=time.time())

        status, body = _get(port,
            "/connections/atlassian_v2/callback?code=x&state=s1")
        assert status == 400
        assert "mismatch" in body.lower()

    def test_callback_preserves_all_params(self, server):
        _, port = server
        with _lock:
            _pending["s2"] = PendingAuth(provider="microsoft", created_at=time.time())

        _get(port,
            "/connections/microsoft/callback?code=abc&state=s2&session_state=xyz")
        with _lock:
            result = _pending["s2"].result
        assert result["code"] == "abc"
        assert result["state"] == "s2"
        assert result["session_state"] == "xyz"


class TestResultEndpoint:
    def test_result_pending(self, server):
        _, port = server
        with _lock:
            _pending["wait"] = PendingAuth(provider="microsoft", created_at=time.time())

        status, body = _get(port, "/auth/result/wait")
        assert status == 200
        assert json.loads(body)["status"] == "pending"

    def test_result_complete(self, server):
        _, port = server
        with _lock:
            _pending["done"] = PendingAuth(
                provider="microsoft", created_at=time.time(),
                result={"code": "abc", "state": "done"})

        status, body = _get(port, "/auth/result/done")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "complete"
        assert data["code"] == "abc"
        # Entry should be deleted after delivery
        with _lock:
            assert "done" not in _pending

    def test_result_unknown_state(self, server):
        _, port = server
        status, _ = _get(port, "/auth/result/nope")
        assert status == 404

    def test_result_deleted_after_delivery(self, server):
        _, port = server
        with _lock:
            _pending["once"] = PendingAuth(
                provider="microsoft", created_at=time.time(),
                result={"code": "x", "state": "once"})

        _get(port, "/auth/result/once")
        status, _ = _get(port, "/auth/result/once")
        assert status == 404


class TestFullFlow:
    def test_register_callback_result_flow(self, server):
        """End-to-end: register → callback → poll result."""
        _, port = server

        # 1. Register
        status, _ = _post_json(port, "/auth/register",
                               {"state": "flow1", "provider": "atlassian_v2"})
        assert status == 200

        # 2. Simulate callback
        status, body = _get(port,
            "/connections/atlassian_v2/callback?code=mycode&state=flow1")
        assert status == 200

        # 3. Poll result
        status, body = _get(port, "/auth/result/flow1")
        data = json.loads(body)
        assert data["status"] == "complete"
        assert data["code"] == "mycode"


class TestExpiry:
    def test_expired_entries_cleaned(self, server):
        _, port = server
        with _lock:
            _pending["old"] = PendingAuth(
                provider="microsoft", created_at=time.time() - 400)

        # Any request triggers cleanup
        _get(port, "/health")
        with _lock:
            assert "old" not in _pending
