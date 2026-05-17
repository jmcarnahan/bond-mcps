"""Tests for proxy_client.py -- OAuthProxyClient."""

import os
from unittest.mock import patch, MagicMock

import pytest

from auth.proxy_client import AuthStateExpiredError, OAuthProxyClient


class TestGetRedirectUri:
    def test_microsoft(self):
        client = OAuthProxyClient(port=8000)
        assert client.get_redirect_uri("microsoft") == \
            "http://localhost:8000/connections/microsoft/callback"

    def test_atlassian(self):
        client = OAuthProxyClient(port=8000)
        assert client.get_redirect_uri("atlassian_v2") == \
            "http://localhost:8000/connections/atlassian_v2/callback"

    def test_custom_port(self):
        client = OAuthProxyClient(port=9999)
        assert client.get_redirect_uri("github") == \
            "http://localhost:9999/connections/github/callback"

    def test_port_from_env(self):
        with patch.dict(os.environ, {"BOND_AUTH_PROXY_PORT": "7777"}):
            client = OAuthProxyClient()
            assert client.port == 7777


class TestHealthCheck:
    def test_healthy(self):
        client = OAuthProxyClient()
        with patch("auth.proxy_client.urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"status": "ok"}'
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            assert client._health_check() is True

    def test_health_rejects_wrong_body(self):
        """A non-proxy service on the port should not pass the health check."""
        client = OAuthProxyClient()
        with patch("auth.proxy_client.urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"healthy": true}'
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            assert client._health_check() is False

    def test_unhealthy(self):
        client = OAuthProxyClient()
        with patch("auth.proxy_client.urllib.request.urlopen",
                   side_effect=OSError("refused")):
            assert client._health_check() is False


class TestCheckProxy:
    def test_succeeds_when_healthy(self):
        client = OAuthProxyClient()
        with patch.object(client, "_health_check", return_value=True):
            client.check_proxy()  # should not raise

    def test_raises_when_unhealthy(self):
        client = OAuthProxyClient()
        with patch.object(client, "_health_check", return_value=False):
            with pytest.raises(RuntimeError, match="not running"):
                client.check_proxy()

    def test_error_message_includes_port(self):
        client = OAuthProxyClient(port=9999)
        with patch.object(client, "_health_check", return_value=False):
            with pytest.raises(RuntimeError, match="9999"):
                client.check_proxy()

    def test_no_auto_start_method(self):
        """Verify _auto_start was removed — proxy must be started manually."""
        client = OAuthProxyClient()
        assert not hasattr(client, "_auto_start")
        assert not hasattr(client, "ensure_running")


class TestWaitForCallback:
    def test_returns_on_complete(self):
        client = OAuthProxyClient()
        responses = [
            {"status": "pending"},
            {"status": "pending"},
            {"status": "complete", "code": "abc", "state": "s1"},
        ]
        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            data = responses[min(call_count["n"], len(responses) - 1)]
            resp.read.return_value = __import__("json").dumps(data).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            call_count["n"] += 1
            return resp

        with patch("auth.proxy_client.urllib.request.urlopen",
                   side_effect=fake_urlopen), \
             patch("auth.proxy_client.time.sleep"):
            result = client.wait_for_callback("s1", timeout=10)

        assert result["code"] == "abc"
        assert "status" not in result  # internal key stripped
        assert call_count["n"] == 3

    def test_timeout_raises(self):
        client = OAuthProxyClient()

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = b'{"status": "pending"}'
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.proxy_client.urllib.request.urlopen",
                   side_effect=fake_urlopen), \
             patch("auth.proxy_client.time.sleep"), \
             patch("auth.proxy_client.time.time",
                   side_effect=[0, 0, 1, 1, 200, 200]):
            with pytest.raises(TimeoutError, match="Timed out"):
                client.wait_for_callback("s1", timeout=5)

    def test_404_raises_state_expired_not_timeout(self):
        """A 404 from /auth/result/<state> means the state was never
        registered or was already consumed — that is NOT a polling timeout."""
        client = OAuthProxyClient()

        def fake_urlopen(req, timeout=None):
            import urllib.error
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, None,
            )

        with patch("auth.proxy_client.urllib.request.urlopen",
                   side_effect=fake_urlopen), \
             patch("auth.proxy_client.time.sleep"):
            with pytest.raises(AuthStateExpiredError):
                client.wait_for_callback("s1", timeout=5)

    def test_auth_state_expired_is_timeout_subclass(self):
        """Existing callers that catch TimeoutError must still see the new
        error so behavior doesn't silently change."""
        assert issubclass(AuthStateExpiredError, TimeoutError)
