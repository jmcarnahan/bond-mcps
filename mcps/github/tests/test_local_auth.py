"""Tests for local_auth.py -- local GitHub OAuth authentication."""

import os
from unittest.mock import patch, MagicMock

import pytest

_TOKEN_STORE_PATCH = "auth.TokenStore"
_PROXY_CLIENT_PATCH = "auth.OAuthProxyClient"


class TestGetLocalToken:
    """Test get_local_token() flow."""

    def test_raises_without_client_id(self):
        from github.local_auth import get_local_token

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="GITHUB_CLIENT_ID"):
                get_local_token()

    def test_returns_cached_token(self):
        from github.local_auth import get_local_token

        mock_store = MagicMock()
        mock_store.get_token.return_value = {"access_token": "cached-tok"}

        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid"}, clear=True), \
             patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("github.local_auth._verify_token", return_value=True):
            token = get_local_token()

        assert token == "cached-tok"

    def test_re_auths_when_cached_token_invalid(self):
        from github.local_auth import get_local_token

        mock_store = MagicMock()
        mock_store.get_token.return_value = {"access_token": "expired-tok"}

        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid"}, clear=True), \
             patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("github.local_auth._verify_token", return_value=False), \
             patch("github.local_auth._do_browser_auth", return_value="new-tok"):
            token = get_local_token()

        assert token == "new-tok"
        mock_store.save_token.assert_called_once()

    def test_falls_back_to_device_code(self):
        from github.local_auth import get_local_token

        mock_store = MagicMock()
        mock_store.get_token.return_value = None

        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid"}, clear=True), \
             patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("github.local_auth._do_browser_auth", return_value=None), \
             patch("github.local_auth._do_device_code_auth", return_value="device-tok"):
            token = get_local_token()

        assert token == "device-tok"

    def test_raises_when_all_flows_fail(self):
        from github.local_auth import get_local_token

        mock_store = MagicMock()
        mock_store.get_token.return_value = None

        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid"}, clear=True), \
             patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("github.local_auth._do_browser_auth", return_value=None), \
             patch("github.local_auth._do_device_code_auth", return_value=None):
            with pytest.raises(PermissionError, match="authentication failed"):
                get_local_token()


class TestDoBrowserAuth:
    """Test _do_browser_auth() with mocked proxy."""

    def test_successful_flow(self):
        from github.local_auth import _do_browser_auth

        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = "http://localhost:8000/connections/github/callback"
        mock_proxy.wait_for_callback.return_value = {
            "code": "authcode", "state": "test-state",
        }

        with patch(_PROXY_CLIENT_PATCH, return_value=mock_proxy), \
             patch("github.local_auth.webbrowser"), \
             patch("github.local_auth.secrets") as mock_secrets, \
             patch("github.local_auth._exchange_code", return_value="new-tok"):
            mock_secrets.token_urlsafe.return_value = "test-state"
            result = _do_browser_auth("cid", "secret")

        assert result == "new-tok"
        mock_proxy.register_auth.assert_called_once()

    def test_returns_none_when_proxy_not_running(self):
        from github.local_auth import _do_browser_auth

        with patch(_PROXY_CLIENT_PATCH) as MockProxy:
            MockProxy.return_value.check_proxy.side_effect = RuntimeError("not running")
            result = _do_browser_auth("cid", "secret")

        assert result is None

    def test_returns_none_on_timeout(self):
        from github.local_auth import _do_browser_auth

        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = "http://localhost:8000/connections/github/callback"
        mock_proxy.wait_for_callback.side_effect = TimeoutError("timed out")

        with patch(_PROXY_CLIENT_PATCH, return_value=mock_proxy), \
             patch("github.local_auth.webbrowser"):
            result = _do_browser_auth("cid", "secret")

        assert result is None
