"""Tests for proxy_server.py -- OAuth callback proxy HTTP server."""

import http.client
import json
import threading
import time
from http.server import HTTPServer

import pytest

from auth.proxy_server import (
    AuthProxyHandler,
    PendingAuth,
    _lock,
    _pending,
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
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
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
        status, body = _post_json(port, "/auth/register", {"state": "abc", "provider": "microsoft"})
        assert status == 200
        assert json.loads(body)["registered"] is True
        assert "abc" in _pending

    def test_register_duplicate_state(self, server):
        _, port = server
        _post_json(port, "/auth/register", {"state": "dup", "provider": "microsoft"})
        status, body = _post_json(port, "/auth/register", {"state": "dup", "provider": "microsoft"})
        assert status == 409

    def test_register_missing_fields(self, server):
        _, port = server
        status, _ = _post_json(port, "/auth/register", {"state": "x"})
        assert status == 400

    def test_register_invalid_json(self, server):
        _, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST", "/auth/register", body=b"not json", headers={"Content-Type": "application/json"}
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()


class TestCallbackEndpoint:
    def test_callback_success(self, server):
        _, port = server
        with _lock:
            _pending["state123"] = PendingAuth(provider="microsoft", created_at=time.time())

        status, body = _get(port, "/connections/microsoft/callback?code=authcode&state=state123")
        assert status == 200
        assert "successful" in body.lower()
        with _lock:
            assert _pending["state123"].result["code"] == "authcode"

    def test_callback_unknown_state(self, server):
        _, port = server
        status, body = _get(port, "/connections/microsoft/callback?code=x&state=unknown")
        assert status == 400
        assert "failed" in body.lower()

    def test_callback_provider_mismatch(self, server):
        _, port = server
        with _lock:
            _pending["s1"] = PendingAuth(provider="microsoft", created_at=time.time())

        status, body = _get(port, "/connections/atlassian_v2/callback?code=x&state=s1")
        assert status == 400
        assert "mismatch" in body.lower()

    def test_callback_preserves_all_params(self, server):
        _, port = server
        with _lock:
            _pending["s2"] = PendingAuth(provider="microsoft", created_at=time.time())

        _get(port, "/connections/microsoft/callback?code=abc&state=s2&session_state=xyz")
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
                provider="microsoft",
                created_at=time.time(),
                result={"code": "abc", "state": "done"},
            )

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
                provider="microsoft", created_at=time.time(), result={"code": "x", "state": "once"}
            )

        _get(port, "/auth/result/once")
        status, _ = _get(port, "/auth/result/once")
        assert status == 404


class TestFullFlow:
    def test_register_callback_result_flow(self, server):
        """End-to-end: register → callback → poll result."""
        _, port = server

        # 1. Register
        status, _ = _post_json(
            port, "/auth/register", {"state": "flow1", "provider": "atlassian_v2"}
        )
        assert status == 200

        # 2. Simulate callback
        status, body = _get(port, "/connections/atlassian_v2/callback?code=mycode&state=flow1")
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
            _pending["old"] = PendingAuth(provider="microsoft", created_at=time.time() - 400)

        # Any request triggers cleanup
        _get(port, "/health")
        with _lock:
            assert "old" not in _pending


class TestPidFile:
    """Pin the PID file location and verify the parent dir is created on demand."""

    def test_default_pid_file_path(self):
        from pathlib import Path

        from auth.proxy_server import PID_FILE

        assert Path.home() / ".bond_mcps" / "auth_proxy.pid" == PID_FILE

    def test_write_pid_creates_parent_dir(self, tmp_path, monkeypatch):
        """A fresh system has no ~/.bond_mcps/; _write_pid_file must mkdir it."""
        from auth import proxy_server as ps

        fake_pid_file = tmp_path / "fresh" / "subdir" / "auth_proxy.pid"
        assert not fake_pid_file.parent.exists()

        monkeypatch.setattr(ps, "PID_FILE", fake_pid_file)
        ps._write_pid_file()

        assert fake_pid_file.exists()
        assert fake_pid_file.read_text().isdigit()
        # Parent dir created with 0700 (owner-only)
        assert (fake_pid_file.parent.stat().st_mode & 0o777) == 0o700


class TestMainDefaultPort:
    """Pin the BOND_AUTH_PROXY_PORT env-var flow on the server side.

    Mirrors test_proxy_client.TestGetRedirectUri.test_port_from_env. Catches
    the round-2 regression where __main__.py argparse default ignored the env
    var and the auth proxy bound to 8000 even when AUTH_PORT=9000 in the
    Makefile.
    """

    def test_default_port_reads_env_var(self, monkeypatch):
        import importlib

        monkeypatch.setenv("BOND_AUTH_PROXY_PORT", "9999")
        import auth.__main__ as main_module

        importlib.reload(main_module)
        try:
            assert main_module.DEFAULT_PORT == 9999
        finally:
            # Restore the unset-env default for other tests
            monkeypatch.delenv("BOND_AUTH_PROXY_PORT", raising=False)
            importlib.reload(main_module)

    def test_default_port_fallback_to_8000(self, monkeypatch):
        import importlib

        monkeypatch.delenv("BOND_AUTH_PROXY_PORT", raising=False)
        import auth.__main__ as main_module

        importlib.reload(main_module)
        assert main_module.DEFAULT_PORT == 8000
