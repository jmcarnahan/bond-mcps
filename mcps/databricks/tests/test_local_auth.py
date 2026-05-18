"""Tests for dbx/local_auth.py — workspace OAuth U2M flow."""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import WORKSPACE_HOST, CLIENT_ID, CLIENT_SECRET

_TOKEN_STORE_PATCH = "auth.TokenStore"
_PROXY_CLIENT_PATCH = "auth.OAuthProxyClient"


class TestHostHelpers:
    def test_normalize_adds_scheme(self):
        from dbx.local_auth import _normalize_host
        assert _normalize_host("dbc-abc.cloud.databricks.com") == "https://dbc-abc.cloud.databricks.com"

    def test_normalize_strips_trailing_slash(self):
        from dbx.local_auth import _normalize_host
        assert _normalize_host("https://dbc-abc.cloud.databricks.com/") == "https://dbc-abc.cloud.databricks.com"

    def test_normalize_preserves_existing_scheme(self):
        from dbx.local_auth import _normalize_host
        assert _normalize_host("http://localhost:8080") == "http://localhost:8080"

    def test_host_without_scheme(self):
        from dbx.local_auth import host_without_scheme
        assert host_without_scheme("https://dbc-abc.cloud.databricks.com") == "dbc-abc.cloud.databricks.com"
        assert host_without_scheme("dbc-abc.cloud.databricks.com") == "dbc-abc.cloud.databricks.com"

    def test_auth_endpoints_use_host(self):
        from dbx.local_auth import _auth_endpoints
        a, t = _auth_endpoints("dbc-abc.cloud.databricks.com")
        assert a == "https://dbc-abc.cloud.databricks.com/oidc/v1/authorize"
        assert t == "https://dbc-abc.cloud.databricks.com/oidc/v1/token"


class TestGetLocalToken:
    def test_raises_without_client_id(self):
        from dbx.local_auth import get_local_token
        with pytest.raises(PermissionError, match="DATABRICKS_CLIENT_ID"):
            get_local_token()

    def test_raises_without_host(self, monkeypatch):
        from dbx.local_auth import get_local_token
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", CLIENT_ID)
        with pytest.raises(PermissionError, match="DATABRICKS_HOST"):
            get_local_token()

    def test_returns_cached_token(self, monkeypatch):
        from dbx.local_auth import get_local_token
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)

        mock_store = MagicMock()
        mock_store.refresh_if_needed.return_value = "cached-oauth-tok"

        with patch(_TOKEN_STORE_PATCH, return_value=mock_store):
            assert get_local_token() == "cached-oauth-tok"

        # Should hit workspace-specific token URL for refresh
        args, _ = mock_store.refresh_if_needed.call_args
        assert args[2] == f"{WORKSPACE_HOST}/oidc/v1/token"

    def test_falls_back_to_browser_when_no_cache(self, monkeypatch):
        from dbx.local_auth import get_local_token
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", CLIENT_SECRET)
        monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)

        mock_store = MagicMock()
        mock_store.refresh_if_needed.return_value = None

        browser_result = {
            "access_token": "browser-tok",
            "refresh_token": "ref-tok",
            "expires_in": 3600,
        }

        with patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("dbx.local_auth._do_browser_auth", return_value=browser_result):
            tok = get_local_token()

        assert tok == "browser-tok"
        mock_store.save_token.assert_called_once()
        saved = mock_store.save_token.call_args[0][0]
        assert saved["access_token"] == "browser-tok"
        assert "expires_at" in saved

    def test_raises_when_browser_auth_fails(self, monkeypatch):
        from dbx.local_auth import get_local_token
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)

        mock_store = MagicMock()
        mock_store.refresh_if_needed.return_value = None

        with patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("dbx.local_auth._do_browser_auth", return_value=None):
            with pytest.raises(PermissionError, match="authentication failed"):
                get_local_token()

    def test_secret_optional(self, monkeypatch):
        """Public OAuth apps don't have a client_secret — flow must still work."""
        from dbx.local_auth import get_local_token
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
        # No CLIENT_SECRET

        mock_store = MagicMock()
        mock_store.refresh_if_needed.return_value = None

        with patch(_TOKEN_STORE_PATCH, return_value=mock_store), \
             patch("dbx.local_auth._do_browser_auth",
                   return_value={"access_token": "tok"}) as do_browser:
            get_local_token()

        # _do_browser_auth must receive None for client_secret in public-app mode
        called_secret = do_browser.call_args[0][1]
        assert called_secret is None


class TestDoBrowserAuth:
    def test_successful_flow(self, monkeypatch):
        from dbx.local_auth import _do_browser_auth

        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = (
            "http://localhost:8000/connections/databricks/callback"
        )
        mock_proxy.wait_for_callback.return_value = {
            "code": "authcode",
            "state": "test-state",
        }

        with patch(_PROXY_CLIENT_PATCH, return_value=mock_proxy), \
             patch("dbx.local_auth.webbrowser"), \
             patch("dbx.local_auth.secrets.token_urlsafe", return_value="test-state"), \
             patch(
                 "dbx.local_auth._exchange_code",
                 return_value={"access_token": "new-tok", "refresh_token": "ref"},
             ):
            result = _do_browser_auth(CLIENT_ID, CLIENT_SECRET, WORKSPACE_HOST)

        assert result == {"access_token": "new-tok", "refresh_token": "ref"}
        mock_proxy.register_auth.assert_called_once()
        # Browser-URL must use the workspace's authorize endpoint
        url_opened = mock_proxy.register_auth.call_args[0]
        assert url_opened[1] == "databricks"

    def test_returns_none_when_proxy_not_running(self):
        from dbx.local_auth import _do_browser_auth
        with patch(_PROXY_CLIENT_PATCH) as MockProxy:
            MockProxy.return_value.check_proxy.side_effect = RuntimeError("not running")
            assert _do_browser_auth(CLIENT_ID, CLIENT_SECRET, WORKSPACE_HOST) is None

    def test_returns_none_on_timeout(self):
        from dbx.local_auth import _do_browser_auth
        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = (
            "http://localhost:8000/connections/databricks/callback"
        )
        mock_proxy.wait_for_callback.side_effect = TimeoutError("timed out")
        with patch(_PROXY_CLIENT_PATCH, return_value=mock_proxy), \
             patch("dbx.local_auth.webbrowser"):
            assert _do_browser_auth(CLIENT_ID, CLIENT_SECRET, WORKSPACE_HOST) is None

    def test_state_mismatch_returns_none(self):
        from dbx.local_auth import _do_browser_auth
        mock_proxy = MagicMock()
        mock_proxy.get_redirect_uri.return_value = (
            "http://localhost:8000/connections/databricks/callback"
        )
        mock_proxy.wait_for_callback.return_value = {
            "code": "x", "state": "DIFFERENT",
        }
        with patch(_PROXY_CLIENT_PATCH, return_value=mock_proxy), \
             patch("dbx.local_auth.webbrowser"), \
             patch("dbx.local_auth.secrets.token_urlsafe", return_value="expected-state"):
            result = _do_browser_auth(CLIENT_ID, CLIENT_SECRET, WORKSPACE_HOST)
        assert result is None


class TestExchangeCode:
    def test_omits_secret_for_public_app(self, respx_mock, monkeypatch):
        """When client_secret is None, the token-exchange body must not
        contain a client_secret field — some IdPs reject empty strings."""
        from dbx.local_auth import _exchange_code

        route = respx_mock.post(f"{WORKSPACE_HOST}/oidc/v1/token").respond(
            200, json={"access_token": "t"}
        )

        result = _exchange_code(
            CLIENT_ID, None, "code-abc", "http://localhost/cb", "verifier", WORKSPACE_HOST
        )
        assert result == {"access_token": "t"}
        assert route.called
        body = route.calls.last.request.content.decode()
        assert "client_secret" not in body
        assert "client_id=" in body
        assert "grant_type=authorization_code" in body
        assert "code_verifier=verifier" in body

    def test_includes_secret_for_confidential_app(self, respx_mock):
        from dbx.local_auth import _exchange_code

        route = respx_mock.post(f"{WORKSPACE_HOST}/oidc/v1/token").respond(
            200, json={"access_token": "t"}
        )

        _exchange_code(
            CLIENT_ID, CLIENT_SECRET, "code-abc", "http://localhost/cb",
            "verifier", WORKSPACE_HOST,
        )
        body = route.calls.last.request.content.decode()
        assert f"client_secret={CLIENT_SECRET}" in body

    def test_returns_none_on_non_2xx(self, respx_mock):
        from dbx.local_auth import _exchange_code
        respx_mock.post(f"{WORKSPACE_HOST}/oidc/v1/token").respond(
            400, json={"error": "invalid_grant"}
        )
        result = _exchange_code(
            CLIENT_ID, CLIENT_SECRET, "code", "http://localhost/cb",
            "verifier", WORKSPACE_HOST,
        )
        assert result is None
