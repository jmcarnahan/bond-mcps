"""Tests for local_auth.py -- local Atlassian OAuth authentication."""

import os
from unittest.mock import patch, MagicMock

import pytest

# Patch targets for lazily-imported auth classes
_TOKEN_STORE_PATCH = "auth.TokenStore"
_PROXY_CLIENT_PATCH = "auth.OAuthProxyClient"


class TestGetLocalTokenAndCloudId:
    """Test get_local_token_and_cloud_id() flow."""

    def test_raises_without_client_id(self):
        from atlassian.local_auth import get_local_token_and_cloud_id

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="ATLASSIAN_CLIENT_ID"):
                get_local_token_and_cloud_id()

    def test_raises_without_client_secret(self):
        from atlassian.local_auth import get_local_token_and_cloud_id

        with patch.dict(os.environ, {"ATLASSIAN_CLIENT_ID": "x"}, clear=True):
            with pytest.raises(PermissionError, match="ATLASSIAN_CLIENT_SECRET"):
                get_local_token_and_cloud_id()

    def test_returns_cached_token(self):
        from atlassian.local_auth import get_local_token_and_cloud_id

        mock_store = MagicMock()
        mock_store.refresh_if_needed.return_value = "cached-tok"
        mock_store.get_token.return_value = {"cloud_id": "cloud-123"}

        with patch.dict(os.environ, {
            "ATLASSIAN_CLIENT_ID": "cid",
            "ATLASSIAN_CLIENT_SECRET": "secret",
        }, clear=True), \
             patch(_TOKEN_STORE_PATCH, return_value=mock_store):
            token, cloud_id = get_local_token_and_cloud_id()

        assert token == "cached-tok"
        assert cloud_id == "cloud-123"

    def test_falls_back_to_browser_when_no_cache(self):
        from atlassian.local_auth import get_local_token_and_cloud_id

        mock_store = MagicMock()
        mock_store.refresh_if_needed.return_value = None
        mock_store.get_token.return_value = None

        with patch.dict(os.environ, {
            "ATLASSIAN_CLIENT_ID": "cid",
            "ATLASSIAN_CLIENT_SECRET": "secret",
            "ATLASSIAN_CLOUD_ID": "env-cloud",
        }, clear=True), \
             patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("atlassian.local_auth._do_browser_auth",
                   return_value={"access_token": "browser-tok", "expires_in": 3600}):
            token, cloud_id = get_local_token_and_cloud_id()

        assert token == "browser-tok"
        assert cloud_id == "env-cloud"
        mock_store.save_token.assert_called_once()

    def test_raises_when_browser_auth_fails(self):
        from atlassian.local_auth import get_local_token_and_cloud_id

        mock_store = MagicMock()
        mock_store.refresh_if_needed.return_value = None
        mock_store.get_token.return_value = None

        with patch.dict(os.environ, {
            "ATLASSIAN_CLIENT_ID": "cid",
            "ATLASSIAN_CLIENT_SECRET": "secret",
        }, clear=True), \
             patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("atlassian.local_auth._do_browser_auth", return_value=None):
            with pytest.raises(PermissionError, match="authentication failed"):
                get_local_token_and_cloud_id()


class TestDoBrowserAuth:
    """Test _do_browser_auth() with mocked proxy."""

    def test_successful_flow(self):
        from atlassian.local_auth import _do_browser_auth

        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = "http://localhost:8000/connections/atlassian_v2/callback"
        mock_proxy.wait_for_callback.return_value = {
            "status": "complete", "code": "authcode", "state": "test-state",
        }

        mock_exchange = MagicMock(return_value={
            "access_token": "new-tok",
            "refresh_token": "ref-tok",
        })

        with patch(_PROXY_CLIENT_PATCH, return_value=mock_proxy), \
             patch("atlassian.local_auth.webbrowser"), \
             patch("atlassian.local_auth.secrets") as mock_secrets, \
             patch("atlassian.local_auth._exchange_code", mock_exchange):
            mock_secrets.token_urlsafe.return_value = "test-state"
            result = _do_browser_auth("cid", "secret")

        assert result == {"access_token": "new-tok", "refresh_token": "ref-tok"}
        mock_proxy.register_auth.assert_called_once()

    def test_returns_none_when_proxy_not_running(self):
        from atlassian.local_auth import _do_browser_auth

        with patch(_PROXY_CLIENT_PATCH) as MockProxy:
            MockProxy.return_value.check_proxy.side_effect = RuntimeError("not running")
            result = _do_browser_auth("cid", "secret")

        assert result is None

    def test_returns_none_on_timeout(self):
        from atlassian.local_auth import _do_browser_auth

        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = "http://localhost:8000/connections/atlassian_v2/callback"
        mock_proxy.wait_for_callback.side_effect = TimeoutError("timed out")

        with patch(_PROXY_CLIENT_PATCH, return_value=mock_proxy), \
             patch("atlassian.local_auth.webbrowser"):
            result = _do_browser_auth("cid", "secret")

        assert result is None
