"""Tests for local_auth.py -- local MSAL authentication."""

import os
from unittest.mock import patch, MagicMock

import pytest


class TestGetLocalToken:
    """Test get_local_token() flow."""

    def test_raises_without_client_id(self):
        from ms_graph.local_auth import get_local_token

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="MS_CLIENT_ID"):
                get_local_token()

    def test_returns_cached_token_silent(self):
        from ms_graph.local_auth import get_local_token

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "user@example.com"}]
        mock_app.acquire_token_silent.return_value = {"access_token": "cached-tok"}

        with patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True), \
             patch("ms_graph.local_auth._create_msal_app", return_value=mock_app), \
             patch("ms_graph.local_auth._load_token_cache"), \
             patch("ms_graph.local_auth._save_token_cache"):
            token = get_local_token()

        assert token == "cached-tok"
        mock_app.acquire_token_silent.assert_called_once()

    def test_falls_back_to_browser_when_no_cache(self):
        from ms_graph.local_auth import get_local_token

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        with patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True), \
             patch("ms_graph.local_auth._create_msal_app", return_value=mock_app), \
             patch("ms_graph.local_auth._load_token_cache"), \
             patch("ms_graph.local_auth._save_token_cache"), \
             patch("ms_graph.local_auth._acquire_token_browser",
                   return_value={"access_token": "browser-tok"}) as mock_browser:
            token = get_local_token()

        assert token == "browser-tok"
        mock_browser.assert_called_once()

    def test_falls_back_to_device_code_when_browser_fails(self):
        from ms_graph.local_auth import get_local_token

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        with patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True), \
             patch("ms_graph.local_auth._create_msal_app", return_value=mock_app), \
             patch("ms_graph.local_auth._load_token_cache"), \
             patch("ms_graph.local_auth._save_token_cache"), \
             patch("ms_graph.local_auth._acquire_token_browser", return_value=None), \
             patch("ms_graph.local_auth._acquire_token_device_code",
                   return_value={"access_token": "device-tok"}) as mock_device:
            token = get_local_token()

        assert token == "device-tok"
        mock_device.assert_called_once()

    def test_raises_when_all_flows_fail(self):
        from ms_graph.local_auth import get_local_token

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        with patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True), \
             patch("ms_graph.local_auth._create_msal_app", return_value=mock_app), \
             patch("ms_graph.local_auth._load_token_cache"), \
             patch("ms_graph.local_auth._acquire_token_browser", return_value=None), \
             patch("ms_graph.local_auth._acquire_token_device_code", return_value=None):
            with pytest.raises(PermissionError, match="authentication failed"):
                get_local_token()

    def test_silent_failure_falls_through_to_browser(self):
        """When acquire_token_silent returns None, browser flow should be tried."""
        from ms_graph.local_auth import get_local_token

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "user@example.com"}]
        mock_app.acquire_token_silent.return_value = None

        with patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True), \
             patch("ms_graph.local_auth._create_msal_app", return_value=mock_app), \
             patch("ms_graph.local_auth._load_token_cache"), \
             patch("ms_graph.local_auth._save_token_cache"), \
             patch("ms_graph.local_auth._acquire_token_browser",
                   return_value={"access_token": "browser-tok"}) as mock_browser:
            token = get_local_token()

        assert token == "browser-tok"
        mock_browser.assert_called_once()


class TestCreateMsalApp:
    def test_creates_public_app_without_secret(self):
        import msal
        from ms_graph.local_auth import _create_msal_app

        cache = msal.SerializableTokenCache()
        with patch.dict(os.environ, {"MS_TENANT_ID": "consumers"}, clear=True):
            app = _create_msal_app("test-id", cache)
        assert isinstance(app, msal.PublicClientApplication)

    def test_creates_confidential_app_with_secret(self):
        import msal
        from ms_graph.local_auth import _create_msal_app

        cache = msal.SerializableTokenCache()
        with patch.dict(os.environ, {
            "MS_CLIENT_SECRET": "test-secret",
            "MS_TENANT_ID": "consumers",
        }, clear=True):
            app = _create_msal_app("test-id", cache)
        assert isinstance(app, msal.ConfidentialClientApplication)


class TestGetScopes:
    def test_consumer_scopes_without_tenant(self):
        from ms_graph.local_auth import _get_scopes

        with patch.dict(os.environ, {}, clear=True):
            scopes = _get_scopes()
        assert "Mail.Read" in scopes
        assert "Files.ReadWrite.All" in scopes
        assert "Team.ReadBasic.All" not in scopes
        assert "Sites.ReadWrite.All" not in scopes

    def test_org_scopes_with_tenant(self):
        from ms_graph.local_auth import _get_scopes

        with patch.dict(os.environ, {"MS_TENANT_ID": "my-tenant"}):
            scopes = _get_scopes()
        assert "Mail.Read" in scopes
        assert "Team.ReadBasic.All" in scopes
        assert "Sites.ReadWrite.All" in scopes


class TestAcquireTokenBrowserProxy:
    """Test _acquire_token_browser() with the shared proxy."""

    def test_uses_proxy_when_available(self):
        from ms_graph.local_auth import _acquire_token_via_proxy

        mock_app = MagicMock()
        mock_app.initiate_auth_code_flow.return_value = {
            "auth_uri": "https://login.microsoftonline.com/authorize",
            "state": "msal-state-123",
        }
        mock_app.acquire_token_by_auth_code_flow.return_value = {
            "access_token": "proxy-tok",
        }

        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = "http://localhost:8000/connections/microsoft/callback"
        mock_proxy.wait_for_callback.return_value = {
            "code": "authcode", "state": "msal-state-123",
        }

        with patch("ms_graph.local_auth.webbrowser"):
            result = _acquire_token_via_proxy(mock_app, ["Mail.Read"], mock_proxy)

        assert result == {"access_token": "proxy-tok"}
        mock_proxy.register_auth.assert_called_once_with("msal-state-123", "microsoft")
        mock_proxy.wait_for_callback.assert_called_once()

    def test_returns_none_when_proxy_not_running(self):
        """When proxy isn't running, _acquire_token_browser returns None."""
        from ms_graph.local_auth import _acquire_token_browser
        import auth

        mock_app = MagicMock()

        with patch.object(auth, "OAuthProxyClient") as MockProxy:
            MockProxy.return_value.check_proxy.side_effect = RuntimeError("not running")
            result = _acquire_token_browser(mock_app, ["Mail.Read"])

        assert result is None

    def test_proxy_timeout_returns_none(self):
        from ms_graph.local_auth import _acquire_token_via_proxy

        mock_app = MagicMock()
        mock_app.initiate_auth_code_flow.return_value = {
            "auth_uri": "https://login.microsoftonline.com/authorize",
            "state": "s1",
        }

        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = "http://localhost:8000/connections/microsoft/callback"
        mock_proxy.wait_for_callback.side_effect = TimeoutError("timed out")

        with patch("ms_graph.local_auth.webbrowser"):
            result = _acquire_token_via_proxy(mock_app, ["Mail.Read"], mock_proxy)

        assert result is None

    def test_proxy_crash_returns_none(self):
        """RuntimeError from proxy (e.g., proxy crashed mid-flow)."""
        from ms_graph.local_auth import _acquire_token_browser
        import auth

        mock_proxy = MagicMock()
        mock_proxy.check_proxy.return_value = None  # passes
        mock_proxy.get_redirect_uri.return_value = "http://localhost:8000/connections/microsoft/callback"
        mock_proxy.wait_for_callback.side_effect = RuntimeError("proxy died")

        mock_app = MagicMock()
        mock_app.initiate_auth_code_flow.return_value = {
            "auth_uri": "https://login.microsoftonline.com/authorize",
            "state": "s1",
        }

        with patch.object(auth, "OAuthProxyClient", return_value=mock_proxy), \
             patch("ms_graph.local_auth.webbrowser"):
            result = _acquire_token_browser(mock_app, ["Mail.Read"])

        assert result is None


class TestTokenCacheSecurity:
    def test_save_sets_0600_permissions(self, tmp_path):
        import ms_graph.local_auth as la

        mock_cache = MagicMock()
        mock_cache.has_state_changed = True
        mock_cache.serialize.return_value = '{"cache": "data"}'

        fake_path = tmp_path / "microsoft.json"
        with patch.object(la, "TOKEN_CACHE_PATH", fake_path):
            la._save_token_cache(mock_cache)

        assert fake_path.exists()
        mode = fake_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_save_skips_when_no_state_change(self, tmp_path):
        import ms_graph.local_auth as la

        mock_cache = MagicMock()
        mock_cache.has_state_changed = False

        fake_path = tmp_path / "microsoft.json"
        with patch.object(la, "TOKEN_CACHE_PATH", fake_path):
            la._save_token_cache(mock_cache)

        assert not fake_path.exists()

    def test_save_creates_parent_dir(self, tmp_path):
        """A fresh system has no ~/.bond_mcps/; _save_token_cache must mkdir it."""
        import ms_graph.local_auth as la

        mock_cache = MagicMock()
        mock_cache.has_state_changed = True
        mock_cache.serialize.return_value = '{"cache": "data"}'

        fake_path = tmp_path / "fresh" / "subdir" / "microsoft.json"
        assert not fake_path.parent.exists()

        with patch.object(la, "TOKEN_CACHE_PATH", fake_path):
            la._save_token_cache(mock_cache)

        assert fake_path.exists()
        # Parent dir created with 0700 (owner-only)
        assert (fake_path.parent.stat().st_mode & 0o777) == 0o700


class TestTokenCacheDefaults:
    """Pin the production default cache location so it can't silently regress."""

    def test_default_token_cache_path(self):
        from pathlib import Path
        from ms_graph.local_auth import TOKEN_CACHE_PATH
        assert TOKEN_CACHE_PATH == Path.home() / ".bond_mcps" / "microsoft.json"
