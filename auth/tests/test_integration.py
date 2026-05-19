"""
Integration tests — OAuthProxyClient against a real ThreadingHTTPServer.

These tests exercise the exact scenario that broke in production: the browser
callback arrives while the client is actively polling wait_for_callback().
With a single-threaded HTTPServer the poll and callback would block each other;
with _ThreadingHTTPServer they are handled concurrently.
"""

import http.client
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from auth.proxy_client import OAuthProxyClient
from auth.proxy_server import (
    AuthProxyHandler,
    _lock,
    _pending,
    _ThreadingHTTPServer,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_state():
    with _lock:
        _pending.clear()
    yield
    with _lock:
        _pending.clear()


@pytest.fixture()
def threading_server():
    """Production-config server: ThreadingHTTPServer on a random port."""
    srv = _ThreadingHTTPServer(("127.0.0.1", 0), AuthProxyHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, port
    srv.shutdown()
    srv.server_close()


def _send_browser_callback(port: int, provider: str, state: str, code: str, delay: float = 0.0):
    """Simulate the browser redirect hitting the proxy after an optional delay."""
    if delay:
        time.sleep(delay)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "GET",
        f"/connections/{provider}/callback?code={code}&state={state}",
    )
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status


# ---------------------------------------------------------------------------
# Core concurrency test — the exact production failure scenario
# ---------------------------------------------------------------------------


class TestConcurrentCallbackAndPolling:
    """Browser callback arrives while client is blocking in wait_for_callback."""

    def test_callback_during_active_polling(self, threading_server):
        """Register → start polling → browser callback arrives → poll returns.

        This is the exact scenario that hung in production with a single-
        threaded HTTPServer. The callback comes in 300 ms after polling begins.
        """
        _, port = threading_server
        client = OAuthProxyClient(port=port)

        client.register_auth("concurrent-state", "microsoft")

        # Fire the callback 300 ms after polling starts
        callback_thread = threading.Thread(
            target=_send_browser_callback,
            args=(port, "microsoft", "concurrent-state", "real-code"),
            kwargs={"delay": 0.3},
            daemon=True,
        )
        callback_thread.start()

        # This blocked forever with single-threaded server
        result = client.wait_for_callback("concurrent-state", timeout=5)

        assert result["code"] == "real-code"
        assert result["state"] == "concurrent-state"
        assert "status" not in result  # internal key stripped for MSAL

        callback_thread.join(timeout=2)

    def test_multiple_concurrent_flows(self, threading_server):
        """Two OAuth flows (different providers) running simultaneously."""
        _, port = threading_server
        client = OAuthProxyClient(port=port)

        client.register_auth("flow-a", "microsoft")
        client.register_auth("flow-b", "atlassian_v2")

        results = {}
        errors = []

        def poll(state):
            try:
                results[state] = client.wait_for_callback(state, timeout=5)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=poll, args=("flow-a",), daemon=True),
            threading.Thread(target=poll, args=("flow-b",), daemon=True),
            threading.Thread(
                target=_send_browser_callback,
                args=(port, "microsoft", "flow-a", "code-a"),
                kwargs={"delay": 0.2},
                daemon=True,
            ),
            threading.Thread(
                target=_send_browser_callback,
                args=(port, "atlassian_v2", "flow-b", "code-b"),
                kwargs={"delay": 0.3},
                daemon=True,
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Unexpected errors: {errors}"
        assert results["flow-a"]["code"] == "code-a"
        assert results["flow-b"]["code"] == "code-b"
        assert "status" not in results["flow-a"]
        assert "status" not in results["flow-b"]


# ---------------------------------------------------------------------------
# End-to-end client ↔ server integration
# ---------------------------------------------------------------------------


class TestClientServerIntegration:
    def test_full_oauth_flow_e2e(self, threading_server):
        """Register → callback → wait_for_callback with a real server."""
        _, port = threading_server
        client = OAuthProxyClient(port=port)

        assert client._health_check() is True

        client.register_auth("e2e-state", "atlassian_v2")

        status = _send_browser_callback(
            port,
            "atlassian_v2",
            "e2e-state",
            "e2e-code",
        )
        assert status == 200

        result = client.wait_for_callback("e2e-state", timeout=5)
        assert result["code"] == "e2e-code"
        assert result["state"] == "e2e-state"
        assert "status" not in result

    def test_callback_preserves_extra_oauth_params(self, threading_server):
        """Extra params (session_state, client_info) are forwarded."""
        _, port = threading_server
        client = OAuthProxyClient(port=port)

        client.register_auth("extra-params", "microsoft")

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/connections/microsoft/callback"
            "?code=c&state=extra-params&session_state=xyz&client_info=abc",
        )
        conn.getresponse()
        conn.close()

        result = client.wait_for_callback("extra-params", timeout=5)
        assert result["code"] == "c"
        assert result["session_state"] == "xyz"
        assert result["client_info"] == "abc"

    def test_poll_timeout_without_callback(self, threading_server):
        """Polling with no callback should raise TimeoutError."""
        _, port = threading_server
        client = OAuthProxyClient(port=port)

        client.register_auth("no-callback", "microsoft")

        with pytest.raises(TimeoutError, match="Timed out"):
            client.wait_for_callback(
                "no-callback",
                timeout=1.5,
                poll_interval=0.2,
            )


# ---------------------------------------------------------------------------
# Health check validation — rejects non-proxy services
# ---------------------------------------------------------------------------


class TestHealthCheckValidation:
    def test_real_proxy_passes_health_check(self, threading_server):
        _, port = threading_server
        client = OAuthProxyClient(port=port)
        assert client._health_check() is True

    def test_non_proxy_server_fails_health_check(self):
        """A generic HTTP server returning 200 must NOT pass the check."""

        class FakeHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 0), FakeHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        try:
            client = OAuthProxyClient(port=port)
            assert client._health_check() is False
        finally:
            srv.shutdown()
            srv.server_close()

    def test_no_server_fails_health_check(self):
        """No server on the port → health check returns False."""
        client = OAuthProxyClient(port=19999)
        assert client._health_check() is False
