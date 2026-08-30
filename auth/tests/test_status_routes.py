"""Tests for the /connect/<provider>/status and /connect/<provider>/token endpoints."""

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from auth.connect_routes import (
    ProviderConnectConfig,
    _check_status,
    _clear_token,
    _mint_ticket,
    _start_connect,
    register_status_routes,
)
from auth.token_store import TokenStore


@pytest.fixture
def config():
    return ProviderConnectConfig(
        name="github",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        scopes="repo",
        client_id_env="TEST_CLIENT_ID",
        client_secret_env="TEST_CLIENT_SECRET",
    )


@pytest.fixture
def store(repo, config):
    return TokenStore(config.name, user_key="alice")


class TestCheckStatus:
    def test_connected_with_token(self, store, config):
        token = {"access_token": "gho_abc", "expires_at": time.time() + 3600}
        store.save_token(token)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True
        assert body["provider"] == "github"
        assert body["expires_at"] == pytest.approx(token["expires_at"], abs=1)
        assert body["token"] == "valid"

    def test_self_heal_refreshes_expired_token(self, store, config):
        """Expired access token + successful refresh -> connected via 'refreshed'."""
        store.save_token(
            {"access_token": "gho_old", "refresh_token": "rt", "expires_at": time.time() - 300}
        )

        def fake_refresh(client_id, client_secret, token_url):
            # Simulate a successful refresh by warming the row.
            store.save_token({"access_token": "gho_new", "expires_at": time.time() + 3600})
            return "gho_new"

        env = patch.dict("os.environ", {"TEST_CLIENT_ID": "id", "TEST_CLIENT_SECRET": "secret"})
        with env, patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            with patch.object(TokenStore, "refresh_if_needed", autospec=True) as m:
                m.side_effect = lambda self, ci, cs, url: fake_refresh(ci, cs, url)
                resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True
        assert body["token"] == "refreshed"
        assert body["expires_at"] is not None

    def test_refresh_failure_reports_reason(self, store, config):
        """Expired token, refresh returns None, but a refresh token exists -> refresh_failed."""
        store.save_token(
            {"access_token": "gho_old", "refresh_token": "rt", "expires_at": time.time() - 300}
        )

        env = patch.dict("os.environ", {"TEST_CLIENT_ID": "id", "TEST_CLIENT_SECRET": "secret"})
        with env, patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            with patch.object(TokenStore, "refresh_if_needed", autospec=True, return_value=None):
                resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        # Merged semantics: a row exists so connected stays True (bond-ai's
        # contract); the fork's "unusable" signal moves to valid/reason.
        assert body["connected"] is True
        assert body["valid"] is True  # refreshable counts as valid
        assert body["reason"] == "refresh_failed"

    def test_refresh_exception_degrades_gracefully(self, store, config):
        """A refresh that raises must not 500; falls through to a reason."""
        store.save_token(
            {"access_token": "gho_old", "refresh_token": "rt", "expires_at": time.time() - 300}
        )

        env = patch.dict("os.environ", {"TEST_CLIENT_ID": "id", "TEST_CLIENT_SECRET": "secret"})
        with env, patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            with patch.object(
                TokenStore, "refresh_if_needed", autospec=True, side_effect=RuntimeError("boom")
            ):
                resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True  # row exists; merged semantics
        assert body["reason"] == "refresh_failed"

    def test_no_client_secret_skips_refresh(self, store, config, monkeypatch):
        """Missing client_id (no secret configured) must not attempt refresh or 500."""
        # Ensure the provider's secret env vars are genuinely absent, so a
        # leaked TEST_CLIENT_ID from another test/shell can't flip this branch.
        monkeypatch.delenv("TEST_CLIENT_ID", raising=False)
        monkeypatch.delenv("TEST_CLIENT_SECRET", raising=False)
        store.save_token(
            {"access_token": "gho_old", "refresh_token": "rt", "expires_at": time.time() - 300}
        )

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            with patch.object(TokenStore, "refresh_if_needed", autospec=True) as m:
                resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True  # row exists; merged semantics
        assert body["reason"] == "refresh_failed"
        m.assert_not_called()

    def test_self_heal_end_to_end_real_wiring(self, store, config):
        """Integration: exercise the full _check_status -> refresh_if_needed ->
        _do_refresh -> urlopen chain (only the network call is mocked). This is
        the test that catches a regression in the args/URL passed to the refresh,
        which the fully-mocked tests above cannot see."""
        store.save_token(
            {"access_token": "gho_old", "refresh_token": "rt123", "expires_at": time.time() - 300}
        )

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data.decode()
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {"access_token": "gho_new", "refresh_token": "rt456", "expires_in": 3600}
            ).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        env = patch.dict("os.environ", {"TEST_CLIENT_ID": "cid", "TEST_CLIENT_SECRET": "csecret"})
        with env, patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
                resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True
        assert body["token"] == "refreshed"
        assert body["expires_at"] is not None
        # The right token URL and credentials were threaded through end to end.
        assert captured["url"] == "https://example.com/token"
        assert "client_id=cid" in captured["body"]
        assert "refresh_token=rt123" in captured["body"]
        # The rotated refresh token was persisted.
        assert store.repo.get_token("alice", config.name)["refresh_token"] == "rt456"

    def test_connected_without_expires_at(self, store, config):
        token = {"access_token": "gho_pat_noexpiry"}
        store.save_token(token)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True
        assert body["provider"] == "github"
        assert body["expires_at"] is None

    def test_not_connected_no_token(self, repo, config):
        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is False
        assert body["provider"] == "github"
        assert body["expires_at"] is None
        assert body["reason"] == "not_connected"

    def test_not_connected_expired_token(self, store, config):
        # Expired access token, no refresh token: the row still exists so
        # connected stays True (merged semantics); valid=False is what marks
        # the connection unusable, and reason says why.
        token = {"access_token": "gho_old", "expires_at": time.time() - 300}
        store.save_token(token)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True
        assert body["valid"] is False
        assert body["reason"] == "not_connected"

    def test_unauthorized_on_runtime_error(self, config):
        with patch(
            "auth.connect_routes.resolve_user_key_for_request",
            side_effect=RuntimeError("bad sub"),
        ):
            resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 401
        assert body["error"] == "unauthorized"

    def test_deployment_config_error_propagates(self, config):
        from auth.db.session import DeploymentConfigError

        with patch(
            "auth.connect_routes.resolve_user_key_for_request",
            side_effect=DeploymentConfigError("BOND_MCPS_USER_ID required"),
        ):
            with pytest.raises(DeploymentConfigError):
                asyncio.run(_check_status(None, config))

    def test_user_isolation(self, repo, config):
        alice_store = TokenStore(config.name, user_key="alice")
        alice_store.save_token({"access_token": "gho_alice", "expires_at": time.time() + 3600})

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="bob"):
            resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert body["connected"] is False


class TestClearToken:
    def test_clears_existing_token(self, store, config):
        store.save_token({"access_token": "gho_abc", "expires_at": time.time() + 3600})

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_clear_token(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["cleared"] is True
        assert body["provider"] == "github"

        assert store.get_token() is None

    def test_idempotent_clear_no_token(self, repo, config):
        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_clear_token(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["cleared"] is True

    def test_unauthorized_on_runtime_error(self, config):
        with patch(
            "auth.connect_routes.resolve_user_key_for_request",
            side_effect=RuntimeError("bad sub"),
        ):
            resp = asyncio.run(_clear_token(None, config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 401
        assert body["error"] == "unauthorized"

    def test_deployment_config_error_propagates(self, config):
        from auth.db.session import DeploymentConfigError

        with patch(
            "auth.connect_routes.resolve_user_key_for_request",
            side_effect=DeploymentConfigError("BOND_MCPS_USER_ID required"),
        ):
            with pytest.raises(DeploymentConfigError):
                asyncio.run(_clear_token(None, config))

    def test_user_isolation(self, repo, config):
        alice_store = TokenStore(config.name, user_key="alice")
        alice_store.save_token({"access_token": "gho_alice", "expires_at": time.time() + 3600})

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            asyncio.run(_clear_token(None, config))

        bob_store = TokenStore(config.name, user_key="bob")
        bob_store.save_token({"access_token": "gho_bob", "expires_at": time.time() + 3600})

        assert bob_store.get_token() is not None


class TestRouteRegistration:
    """Integration test: verify routes are wired correctly via register_status_routes."""

    def test_status_get_route(self, repo, config):
        from starlette.applications import Starlette
        from starlette.routing import Route

        app = Starlette()

        class FakeMCP:
            def custom_route(self, path, methods=None):
                def decorator(fn):
                    app.routes.append(Route(path, fn, methods=methods))
                    return fn

                return decorator

        mcp = FakeMCP()
        register_status_routes(mcp, config)

        client = TestClient(app)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = client.get("/connect/github/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is False
        assert body["provider"] == "github"

    def test_token_delete_route(self, repo, config):
        from starlette.applications import Starlette
        from starlette.routing import Route

        app = Starlette()

        class FakeMCP:
            def custom_route(self, path, methods=None):
                def decorator(fn):
                    app.routes.append(Route(path, fn, methods=methods))
                    return fn

                return decorator

        mcp = FakeMCP()
        register_status_routes(mcp, config)

        client = TestClient(app)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = client.delete("/connect/github/token")

        assert resp.status_code == 200
        body = resp.json()
        assert body["cleared"] is True

    def test_wrong_method_rejected(self, repo, config):
        from starlette.applications import Starlette
        from starlette.routing import Route

        app = Starlette()

        class FakeMCP:
            def custom_route(self, path, methods=None):
                def decorator(fn):
                    app.routes.append(Route(path, fn, methods=methods))
                    return fn

                return decorator

        mcp = FakeMCP()
        register_status_routes(mcp, config)

        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/connect/github/status")
        assert resp.status_code == 405

    def test_repeated_registration_is_idempotent_per_instance(self, repo, config):
        """register_connect_routes registers the status routes itself, and the
        explicit register_status_routes call sites in the MCP modules run right
        after it — the guard must collapse the duplicates for the SAME mcp
        instance while a fresh instance still gets its own routes (the guard is
        an attribute on the instance, never id(mcp), which CPython recycles)."""
        from starlette.routing import Route

        def make_mcp(routes):
            class FakeMCP:
                def custom_route(self, path, methods=None):
                    def decorator(fn):
                        routes.append(Route(path, fn, methods=methods))
                        return fn

                    return decorator

            return FakeMCP()

        routes_a: list = []
        mcp_a = make_mcp(routes_a)
        register_status_routes(mcp_a, config)
        register_status_routes(mcp_a, config)  # explicit second call: no-op
        status_paths = [r.path for r in routes_a if r.path == "/connect/github/status"]
        assert len(status_paths) == 1

        # A different instance registers independently even after the first
        # is gone (id() would be recyclable here).
        del mcp_a
        routes_b: list = []
        mcp_b = make_mcp(routes_b)
        register_status_routes(mcp_b, config)
        assert [r.path for r in routes_b if r.path == "/connect/github/status"]

    def test_connect_routes_include_status_and_local_callback(self, repo, config, monkeypatch):
        """register_connect_routes provides the status/token routes itself and,
        in local mode only, the /connect/<n>/callback proxy-relay target."""
        from unittest.mock import patch as _patch

        from starlette.routing import Route

        from auth.connect_routes import register_connect_routes

        def collect(jwt_key: str) -> list[str]:
            routes: list = []

            class FakeMCP:
                def custom_route(self, path, methods=None):
                    def decorator(fn):
                        routes.append(Route(path, fn, methods=methods))
                        return fn

                    return decorator

            with _patch.dict(
                "os.environ",
                {"BOND_MCPS_JWT_JWKS_URI": "", "BOND_MCPS_JWT_PUBLIC_KEY": jwt_key},
            ):
                register_connect_routes(FakeMCP(), config)
            return [r.path for r in routes]

        local_paths = collect(jwt_key="")
        assert "/connect/github/status" in local_paths
        assert "/connect/github/token" in local_paths
        assert "/connect/github/callback" in local_paths  # proxy relay target

        jwt_paths = collect(jwt_key="test-key")
        assert "/connect/github/status" in jwt_paths
        # JWT mode keeps the canonical /connections path exclusively.
        assert "/connect/github/callback" not in jwt_paths
        assert "/connections/github/callback" in jwt_paths


@pytest.fixture
def microsoft_config():
    return ProviderConnectConfig(
        name="microsoft",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes="openid profile",
        client_id_env="MS_CLIENT_ID",
        client_secret_env="MS_CLIENT_SECRET",
    )


class TestMsalStatus:
    """Tests for MSAL cache fallback in _check_status for Microsoft providers."""

    def test_connected_via_msal_cache_with_refresh_token(self, repo, microsoft_config):
        msal_cache = json.dumps(
            {
                "RefreshToken": {"rt-entry-1": {"secret": "rt_value"}},
                "AccessToken": {},
                "Account": {},
            }
        )
        repo.save_msal_cache("alice", msal_cache)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, microsoft_config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 200
        assert body["connected"] is True
        assert body["provider"] == "microsoft"
        assert body["expires_at"] is None
        assert body["token"] == "msal"

    def test_connected_via_msal_cache_with_valid_access_token(self, repo, microsoft_config):
        msal_cache = json.dumps(
            {
                "RefreshToken": {},
                "AccessToken": {
                    "at-entry-1": {"secret": "at_value", "expires_on": str(int(time.time()) + 3600)}
                },
                "Account": {},
            }
        )
        repo.save_msal_cache("alice", msal_cache)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, microsoft_config))

        body = json.loads(resp.body.decode())
        assert body["connected"] is True

    def test_not_connected_empty_msal_cache(self, repo, microsoft_config):
        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, microsoft_config))

        body = json.loads(resp.body.decode())
        assert body["connected"] is False

    def test_not_connected_msal_cache_only_expired_tokens(self, repo, microsoft_config):
        msal_cache = json.dumps(
            {
                "RefreshToken": {},
                "AccessToken": {
                    "at-entry-1": {"secret": "at_value", "expires_on": str(int(time.time()) - 3600)}
                },
                "Account": {},
            }
        )
        repo.save_msal_cache("alice", msal_cache)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, microsoft_config))

        body = json.loads(resp.body.decode())
        assert body["connected"] is False

    def test_provider_tokens_takes_precedence_over_msal(self, repo, microsoft_config):
        token = {"access_token": "graph_token", "expires_at": time.time() + 3600}
        TokenStore(microsoft_config.name, user_key="alice").save_token(token)

        msal_cache = json.dumps(
            {
                "RefreshToken": {"rt-entry-1": {"secret": "rt_value"}},
            }
        )
        repo.save_msal_cache("alice", msal_cache)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, microsoft_config))

        body = json.loads(resp.body.decode())
        assert body["connected"] is True
        assert body["expires_at"] == pytest.approx(token["expires_at"], abs=1)

    def test_msal_not_checked_for_non_microsoft_provider(self, repo, config):
        msal_cache = json.dumps(
            {
                "RefreshToken": {"rt-entry-1": {"secret": "rt_value"}},
            }
        )
        repo.save_msal_cache("alice", msal_cache)

        with patch("auth.connect_routes.resolve_user_key_for_request", return_value="alice"):
            resp = asyncio.run(_check_status(None, config))

        body = json.loads(resp.body.decode())
        assert body["connected"] is False


class TestProxyModeConnectRoutes:
    """Tests for connect routes behavior in proxy mode (no JWT)."""

    def test_mint_ticket_returns_404_without_jwt(self, microsoft_config):
        with patch.dict(
            "os.environ",
            {"BOND_MCPS_JWT_JWKS_URI": "", "BOND_MCPS_JWT_PUBLIC_KEY": ""},
        ):
            resp = asyncio.run(_mint_ticket(None, microsoft_config))

        body = json.loads(resp.body.decode())
        assert resp.status_code == 404
        assert body["error"] == "not_found"

    def test_start_connect_no_ticket_needed_proxy_mode(self, repo, microsoft_config):
        from starlette.datastructures import QueryParams

        class FakeRequest:
            query_params = QueryParams("")

        env_patch = patch.dict(
            "os.environ",
            {
                "BOND_MCPS_JWT_JWKS_URI": "",
                "BOND_MCPS_JWT_PUBLIC_KEY": "",
                "MS_CLIENT_ID": "test-client-id",
                "MS_CLIENT_SECRET": "test-client-secret",
                "BOND_MCPS_PUBLIC_URL": "http://localhost:18001",
            },
        )
        with env_patch, patch("auth.connect_routes.current_user_key", return_value="alice"):
            resp = asyncio.run(_start_connect(FakeRequest(), microsoft_config))

        assert resp.status_code == 302
        assert "login.microsoftonline.com" in resp.headers["location"]

    def test_start_connect_requires_ticket_jwt_mode(self, microsoft_config):
        from starlette.datastructures import QueryParams

        class FakeRequest:
            query_params = QueryParams("")

        env_patch = patch.dict(
            "os.environ",
            {"BOND_MCPS_JWT_JWKS_URI": "https://example.com/.well-known/jwks.json"},
        )
        with env_patch:
            resp = asyncio.run(_start_connect(FakeRequest(), microsoft_config))

        assert resp.status_code == 400
        assert b"Missing ticket" in resp.body

    def test_extra_authorize_params_merged_in_redirect(self, repo):
        from urllib.parse import parse_qs, urlparse

        from starlette.datastructures import QueryParams

        config = ProviderConnectConfig(
            name="atlassian",
            authorize_url="https://auth.atlassian.com/authorize",
            token_url="https://auth.atlassian.com/oauth/token",
            scopes="read:jira-user offline_access",
            client_id_env="TEST_CLIENT_ID",
            client_secret_env="TEST_CLIENT_SECRET",
            extra_authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
        )

        class FakeRequest:
            query_params = QueryParams("")

        env_patch = patch.dict(
            "os.environ",
            {
                "BOND_MCPS_JWT_JWKS_URI": "",
                "BOND_MCPS_JWT_PUBLIC_KEY": "",
                "TEST_CLIENT_ID": "test-id",
                "TEST_CLIENT_SECRET": "test-secret",
                "BOND_MCPS_PUBLIC_URL": "http://localhost:18003",
            },
        )
        with env_patch, patch("auth.connect_routes.current_user_key", return_value="alice"):
            resp = asyncio.run(_start_connect(FakeRequest(), config))

        assert resp.status_code == 302
        location = resp.headers["location"]
        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        assert params["audience"] == ["api.atlassian.com"]
        assert params["prompt"] == ["consent"]
