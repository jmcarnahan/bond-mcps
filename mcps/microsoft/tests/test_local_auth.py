"""Tests for local_auth.py -- local MSAL authentication."""

import os
from unittest.mock import MagicMock, patch

import pytest


class TestGetLocalToken:
    """Test get_local_token() resolution order.

    Tests mock at the silent-lock / interactive-fallback boundary; the
    repository-layer behavior is covered separately in the auth package's
    test_repository.py and test_msal_race.py.
    """

    def test_raises_without_client_id(self):
        from ms_graph.local_auth import get_local_token

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="MS_CLIENT_ID"):
                get_local_token()

    def test_returns_cached_token_silent(self):
        from ms_graph.local_auth import get_local_token

        with (
            patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True),
            patch(
                "ms_graph.local_auth._try_silent_under_lock", return_value="cached-tok"
            ) as silent,
            patch("ms_graph.local_auth._acquire_token_interactive") as interactive,
        ):
            token = get_local_token()

        assert token == "cached-tok"
        silent.assert_called_once()
        interactive.assert_not_called()

    def test_falls_back_to_interactive_when_silent_returns_none(self):
        from ms_graph.local_auth import get_local_token

        with (
            patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True),
            patch("ms_graph.local_auth._try_silent_under_lock", return_value=None),
            patch(
                "ms_graph.local_auth._acquire_token_interactive", return_value="browser-tok"
            ) as interactive,
        ):
            token = get_local_token()

        assert token == "browser-tok"
        interactive.assert_called_once()

    def test_raises_when_all_flows_fail(self):
        from ms_graph.local_auth import get_local_token

        with (
            patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}, clear=True),
            patch("ms_graph.local_auth._try_silent_under_lock", return_value=None),
            patch("ms_graph.local_auth._acquire_token_interactive", return_value=None),
        ):
            with pytest.raises(PermissionError, match="authentication failed"):
                get_local_token()


class TestAcquireTokenInteractive:
    """Test _acquire_token_interactive: browser → device-code fallback chain."""

    def test_uses_browser_when_available(self):
        from ms_graph.local_auth import _acquire_token_interactive

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        with (
            patch("ms_graph.local_auth._load_token_cache"),
            patch("ms_graph.local_auth._save_token_cache"),
            patch("ms_graph.local_auth._create_msal_app", return_value=mock_app),
            patch(
                "ms_graph.local_auth._acquire_token_browser",
                return_value={"access_token": "browser-tok"},
            ) as mock_browser,
        ):
            token = _acquire_token_interactive("cid", ["scope"])

        assert token == "browser-tok"
        mock_browser.assert_called_once()

    def test_falls_back_to_device_when_browser_fails(self):
        from ms_graph.local_auth import _acquire_token_interactive

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        with (
            patch("ms_graph.local_auth._load_token_cache"),
            patch("ms_graph.local_auth._save_token_cache"),
            patch("ms_graph.local_auth._create_msal_app", return_value=mock_app),
            patch("ms_graph.local_auth._acquire_token_browser", return_value=None),
            patch(
                "ms_graph.local_auth._acquire_token_device_code",
                return_value={"access_token": "device-tok"},
            ) as mock_device,
        ):
            token = _acquire_token_interactive("cid", ["scope"])

        assert token == "device-tok"
        mock_device.assert_called_once()

    def test_returns_none_when_both_fail(self):
        from ms_graph.local_auth import _acquire_token_interactive

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        with (
            patch("ms_graph.local_auth._load_token_cache"),
            patch("ms_graph.local_auth._save_token_cache"),
            patch("ms_graph.local_auth._create_msal_app", return_value=mock_app),
            patch("ms_graph.local_auth._acquire_token_browser", return_value=None),
            patch("ms_graph.local_auth._acquire_token_device_code", return_value=None),
        ):
            token = _acquire_token_interactive("cid", ["scope"])

        assert token is None


class TestTrySilentUnderLock:
    """_try_silent_under_lock holds the MSAL cache lock around silent acquisition.

    Uses a stub repository so we can assert the locked context is entered
    without standing up a real DB here. End-to-end concurrency is covered
    in auth/tests/test_msal_race.py.
    """

    def test_returns_none_when_no_accounts(self):
        from ms_graph.local_auth import _try_silent_under_lock

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []

        fake_handle = MagicMock(blob=None)
        fake_repo = MagicMock()
        fake_repo.locked_msal_cache.return_value.__enter__.return_value = fake_handle
        fake_repo.locked_msal_cache.return_value.__exit__.return_value = False

        with (
            patch("ms_graph.local_auth._get_repo", return_value=fake_repo),
            patch("ms_graph.local_auth._user_key", return_value="alice"),
            patch("ms_graph.local_auth._create_msal_app", return_value=mock_app),
        ):
            assert _try_silent_under_lock("cid", ["scope"]) is None

        fake_repo.locked_msal_cache.assert_called_once_with("alice")

    def test_returns_token_and_persists_changed_cache(self):
        import msal as _msal
        from ms_graph.local_auth import _try_silent_under_lock

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "u@x"}]
        mock_app.acquire_token_silent.return_value = {"access_token": "silent-tok"}

        fake_handle = MagicMock(blob=None)
        fake_repo = MagicMock()
        fake_repo.locked_msal_cache.return_value.__enter__.return_value = fake_handle
        fake_repo.locked_msal_cache.return_value.__exit__.return_value = False

        # Force cache.has_state_changed to True after MSAL "writes" to it.
        # In the real flow MSAL mutates the cache during acquire_token_silent;
        # here we just have to trigger the set_blob branch.
        class DummyCache(_msal.SerializableTokenCache):
            def __init__(self):
                super().__init__()
                self._fake_changed = True

            @property
            def has_state_changed(self):
                return self._fake_changed

            def serialize(self):
                return '{"AccessToken": {"x": "y"}}'

        with (
            patch("ms_graph.local_auth._get_repo", return_value=fake_repo),
            patch("ms_graph.local_auth._user_key", return_value="alice"),
            patch("ms_graph.local_auth._create_msal_app", return_value=mock_app),
            patch(
                "ms_graph.local_auth.msal.SerializableTokenCache", side_effect=lambda: DummyCache()
            ),
        ):
            token = _try_silent_under_lock("cid", ["scope"])

        assert token == "silent-tok"
        fake_handle.set_blob.assert_called_once_with('{"AccessToken": {"x": "y"}}')


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
        with patch.dict(
            os.environ,
            {
                "MS_CLIENT_SECRET": "test-secret",
                "MS_TENANT_ID": "consumers",
            },
            clear=True,
        ):
            app = _create_msal_app("test-id", cache)
        assert isinstance(app, msal.ConfidentialClientApplication)


class TestLoginScopes:
    def test_consumer_scopes_without_tenant(self):
        """No admin consent exists for consumer accounts, so nothing can wall
        the request — they keep the full mail/files/calendar feature set."""
        from ms_graph.local_auth import login_scopes

        with patch.dict(os.environ, {}, clear=True):
            scopes = login_scopes()
        assert "Mail.Read" in scopes
        assert "Files.ReadWrite.All" in scopes
        assert "Team.ReadBasic.All" not in scopes
        assert "Sites.ReadWrite.All" not in scopes

    def test_org_default_is_exactly_the_consented_set(self):
        """An org request is one consent bundle: a single admin-gated scope in
        it blocks the WHOLE sign-in, mail included. The default must therefore
        be exactly what the admin has consented — nothing aspirational."""
        from ms_graph.local_auth import CONSENTED_ORG_SCOPES, login_scopes

        with patch.dict(os.environ, {"MS_TENANT_ID": "my-tenant"}, clear=True):
            scopes = login_scopes()
        assert scopes == CONSENTED_ORG_SCOPES
        for gated in (
            "Chat.ReadWrite",
            "ChannelMessage.Read.All",
            "ChannelMessage.Send",
            "Channel.ReadBasic.All",
            "Team.ReadBasic.All",
            "Sites.ReadWrite.All",
            "Files.ReadWrite.All",
            "Calendars.ReadWrite",
            "MailboxSettings.ReadWrite",
            "Mail.Read.Shared",
            "Mail.ReadWrite.Shared",
            "Mail.Send.Shared",
        ):
            assert gated not in scopes, gated

    def test_ms_scopes_env_wins_verbatim(self):
        """The escape hatch for a tenant whose admin approved more: widening is
        a config change, never a code change."""
        from ms_graph.local_auth import login_scopes

        with patch.dict(
            os.environ,
            {"MS_TENANT_ID": "my-tenant", "MS_SCOPES": "Mail.Read Chat.ReadWrite"},
            clear=True,
        ):
            assert login_scopes() == ["Mail.Read", "Chat.ReadWrite"]


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
        mock_proxy.get_redirect_uri.return_value = (
            "http://localhost:8000/connections/microsoft/callback"
        )
        mock_proxy.wait_for_callback.return_value = {
            "code": "authcode",
            "state": "msal-state-123",
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
        mock_proxy.get_redirect_uri.return_value = (
            "http://localhost:8000/connections/microsoft/callback"
        )
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
        mock_proxy.get_redirect_uri.return_value = (
            "http://localhost:8000/connections/microsoft/callback"
        )
        mock_proxy.wait_for_callback.side_effect = RuntimeError("proxy died")

        mock_app = MagicMock()
        mock_app.initiate_auth_code_flow.return_value = {
            "auth_uri": "https://login.microsoftonline.com/authorize",
            "state": "s1",
        }

        with (
            patch.object(auth, "OAuthProxyClient", return_value=mock_proxy),
            patch("ms_graph.local_auth.webbrowser"),
        ):
            result = _acquire_token_browser(mock_app, ["Mail.Read"])

        assert result is None


class TestMsalCachePersistence:
    """The MSAL cache is now persisted to the encrypted DB via TokenRepository."""

    def test_save_calls_repository_when_state_changed(self):
        import ms_graph.local_auth as la

        mock_cache = MagicMock()
        mock_cache.has_state_changed = True
        mock_cache.serialize.return_value = '{"cache": "data"}'

        fake_repo = MagicMock()
        with (
            patch.object(la, "_get_repo", return_value=fake_repo),
            patch.object(la, "_user_key", return_value="alice"),
        ):
            la._save_token_cache(mock_cache)

        fake_repo.save_msal_cache.assert_called_once_with("alice", '{"cache": "data"}')

    def test_save_skips_when_no_state_change(self):
        import ms_graph.local_auth as la

        mock_cache = MagicMock()
        mock_cache.has_state_changed = False

        fake_repo = MagicMock()
        with (
            patch.object(la, "_get_repo", return_value=fake_repo),
            patch.object(la, "_user_key", return_value="alice"),
        ):
            la._save_token_cache(mock_cache)

        fake_repo.save_msal_cache.assert_not_called()

    def test_load_deserializes_blob_from_repository(self):
        import json

        import ms_graph.local_auth as la

        blob = '{"AccessToken": {"acct-x": {"credential_type": "AccessToken"}}}'
        fake_repo = MagicMock()
        fake_repo.get_msal_cache.return_value = blob
        with (
            patch.object(la, "_get_repo", return_value=fake_repo),
            patch.object(la, "_user_key", return_value="alice"),
        ):
            cache = la._load_token_cache()

        fake_repo.get_msal_cache.assert_called_once_with("alice")
        # MSAL re-serializes with indentation; compare parsed structure.
        assert json.loads(cache.serialize()) == json.loads(blob)

    def test_load_with_no_existing_row_returns_empty_cache(self):
        import ms_graph.local_auth as la

        fake_repo = MagicMock()
        fake_repo.get_msal_cache.return_value = None
        with (
            patch.object(la, "_get_repo", return_value=fake_repo),
            patch.object(la, "_user_key", return_value="alice"),
        ):
            cache = la._load_token_cache()

        # Empty MSAL cache serializes to a small JSON-ish value or empty string
        assert cache.has_state_changed is False
