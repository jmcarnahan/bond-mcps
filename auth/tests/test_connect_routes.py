"""Tests for the per-MCP /connect delegation routes (return_url, status, disconnect).

Handlers are exercised directly (async via ``asyncio.run``) with a mocked
``get_access_token`` and the in-process SQLite DB from conftest (``repo``
fixture). This mirrors the contract bond-ai's mcp_connect_client expects.
"""

import asyncio
import json
import time
import types

from auth.connect_routes import (
    ProviderConnectConfig,
    _connect_result,
    _connect_status,
    _consume_pkce,
    _default_token_shape,
    _disconnect,
    _finish_connect,
    _mint_ticket,
    _public_base,
    _redirect_uri,
    _stash_pkce,
    _validate_return_url,
)
from auth.db.repository import TokenRepository

CFG = ProviderConnectConfig(
    name="atlassian",
    authorize_url="https://auth.example/authorize",
    token_url="https://auth.example/token",
    scopes="read write",
    client_id_env="X_CLIENT_ID",
    client_secret_env="X_CLIENT_SECRET",
)

USER = "alice@example.com"


class FakeRequest:
    def __init__(self, query=None, body=None):
        self.query_params = query or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _run(coro):
    return asyncio.run(coro)


def _mock_user(monkeypatch, sub=USER):
    access = types.SimpleNamespace(claims={"sub": sub}) if sub is not None else None
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: access)


def _body(resp):
    return json.loads(bytes(resp.body))


# ---------------------------------------------------------------------------
# _validate_return_url
# ---------------------------------------------------------------------------


class TestValidateReturnUrl:
    def test_allowed_hosts(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost,app.example.com")
        assert _validate_return_url("http://localhost:8000/connections") is True
        assert _validate_return_url("https://app.example.com/x") is True

    def test_rejects_unlisted_host(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        assert _validate_return_url("http://evil.com/x") is False

    def test_rejects_non_http_scheme(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        assert _validate_return_url("ftp://localhost/x") is False

    def test_rejects_empty(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        assert _validate_return_url("") is False
        assert _validate_return_url(None) is False

    def test_empty_allowlist_rejects_all(self, monkeypatch):
        monkeypatch.delenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", raising=False)
        assert _validate_return_url("http://localhost/x") is False


# ---------------------------------------------------------------------------
# _public_base
# ---------------------------------------------------------------------------


class TestPublicBase:
    def test_prefers_connect_public_url(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_CONNECT_PUBLIC_URL", "http://localhost:8000")
        monkeypatch.setenv("BOND_MCPS_PUBLIC_URL", "http://localhost:18003")
        assert _public_base(CFG) == "http://localhost:8000"

    def test_falls_back_to_public_url_env(self, monkeypatch):
        monkeypatch.delenv("BOND_MCPS_CONNECT_PUBLIC_URL", raising=False)
        monkeypatch.setenv("BOND_MCPS_PUBLIC_URL", "http://localhost:18003")
        assert _public_base(CFG) == "http://localhost:18003"

    def test_redirect_uri_uses_canonical_connections_path(self, monkeypatch):
        # The provider redirect_uri must be the already-registered
        # /connections/<name>/callback path, NOT /connect/<name>/callback.
        monkeypatch.setenv("BOND_MCPS_CONNECT_PUBLIC_URL", "http://localhost:8000")
        assert _redirect_uri(CFG) == "http://localhost:8000/connections/atlassian/callback"


# ---------------------------------------------------------------------------
# _connect_result (302 round-trip vs terminal HTML)
# ---------------------------------------------------------------------------


class TestConnectResult:
    def test_success_redirect(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        resp = _connect_result(
            "http://localhost:8000/connections", "atlassian", ok=True, html_msg="x"
        )
        assert resp.status_code == 302
        assert (
            resp.headers["location"]
            == "http://localhost:8000/connections?connection_success=atlassian"
        )

    def test_error_redirect(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        resp = _connect_result(
            "http://localhost:8000/connections",
            "atlassian",
            ok=False,
            error="token_exchange_failed",
            html_status=502,
            html_msg="x",
        )
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert "connection_error=atlassian" in loc
        assert "error=token_exchange_failed" in loc

    def test_separator_when_query_already_present(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        resp = _connect_result("http://localhost:8000/c?x=1", "atlassian", ok=True, html_msg="x")
        assert resp.headers["location"].endswith("&connection_success=atlassian")

    def test_html_when_no_return_url(self):
        resp = _connect_result(None, "atlassian", ok=True, html_msg="done")
        assert resp.status_code == 200
        assert b"done" in resp.body

    def test_html_when_return_url_not_allowed(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        resp = _connect_result(
            "http://evil.com", "atlassian", ok=False, html_status=502, html_msg="bad"
        )
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# _default_token_shape  (scope -> scopes persistence key)
# ---------------------------------------------------------------------------


class TestDefaultTokenShape:
    def test_maps_scope_to_scopes(self):
        out = _default_token_shape(
            {
                "access_token": "t",
                "scope": "read write",
                "refresh_token": "r",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )
        assert out["access_token"] == "t"
        assert out["scopes"] == "read write"
        assert "scope" not in out
        assert out["refresh_token"] == "r"
        assert out["expires_at"] > time.time()
        assert out["token_type"] == "Bearer"


# ---------------------------------------------------------------------------
# return_url stash/consume round-trip (DB)
# ---------------------------------------------------------------------------


class TestStashConsumeReturnUrl:
    def test_round_trip_carries_return_url(self, repo):
        _stash_pkce(
            state="s1",
            user_key=USER,
            code_verifier="v1",
            provider="atlassian",
            return_url="http://localhost:8000/connections",
        )
        out = _consume_pkce(state="s1", provider="atlassian")
        assert out == {
            "user_key": USER,
            "code_verifier": "v1",
            "return_url": "http://localhost:8000/connections",
        }

    def test_round_trip_without_return_url(self, repo):
        _stash_pkce(state="s2", user_key=USER, code_verifier="v", provider="atlassian")
        out = _consume_pkce(state="s2", provider="atlassian")
        assert out["return_url"] is None


# ---------------------------------------------------------------------------
# GET /connect/<name>/status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_not_connected(self, repo, monkeypatch):
        _mock_user(monkeypatch)
        resp = _run(_connect_status(FakeRequest(), CFG))
        assert _body(resp) == {"connected": False, "valid": True, "scopes": None}

    def test_connected_valid(self, repo, monkeypatch):
        repo.save_token(
            USER,
            "atlassian",
            {
                "access_token": "t",
                "scopes": "read write",
                "expires_at": time.time() + 3600,
                "refresh_token": "r",
            },
        )
        _mock_user(monkeypatch)
        data = _body(_run(_connect_status(FakeRequest(), CFG)))
        assert data["connected"] is True
        assert data["valid"] is True
        assert data["scopes"] == "read write"

    def test_expired_with_refresh_is_valid(self, repo, monkeypatch):
        repo.save_token(
            USER,
            "atlassian",
            {"access_token": "t", "expires_at": time.time() - 10, "refresh_token": "r"},
        )
        _mock_user(monkeypatch)
        data = _body(_run(_connect_status(FakeRequest(), CFG)))
        assert data["connected"] is True
        assert data["valid"] is True

    def test_expired_without_refresh_is_invalid(self, repo, monkeypatch):
        repo.save_token(USER, "atlassian", {"access_token": "t", "expires_at": time.time() - 10})
        _mock_user(monkeypatch)
        data = _body(_run(_connect_status(FakeRequest(), CFG)))
        assert data["connected"] is True
        assert data["valid"] is False

    def test_unauthenticated_401(self, repo, monkeypatch):
        _mock_user(monkeypatch, sub=None)
        resp = _run(_connect_status(FakeRequest(), CFG))
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /connect/<name>
# ---------------------------------------------------------------------------


class TestDisconnect:
    def test_disconnect_existing(self, repo, monkeypatch):
        repo.save_token(USER, "atlassian", {"access_token": "t"})
        _mock_user(monkeypatch)
        resp = _run(_disconnect(FakeRequest(), CFG))
        assert _body(resp) == {"disconnected": True}
        assert repo.get_token(USER, "atlassian") is None

    def test_disconnect_absent(self, repo, monkeypatch):
        _mock_user(monkeypatch)
        resp = _run(_disconnect(FakeRequest(), CFG))
        assert _body(resp) == {"disconnected": False}


# ---------------------------------------------------------------------------
# POST /connect/<name>/ticket
# ---------------------------------------------------------------------------


class TestMintTicket:
    def test_mint_includes_return_url(self, repo, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        monkeypatch.setenv("BOND_MCPS_CONNECT_PUBLIC_URL", "http://localhost:8000")
        _mock_user(monkeypatch)
        resp = _run(
            _mint_ticket(FakeRequest(body={"return_url": "http://localhost:8000/connections"}), CFG)
        )
        data = _body(resp)
        assert data["connect_url"].startswith("http://localhost:8000/connect/atlassian?ticket=")
        assert "return_url=" in data["connect_url"]

    def test_mint_rejects_bad_return_url(self, repo, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        monkeypatch.setenv("BOND_MCPS_CONNECT_PUBLIC_URL", "http://localhost:8000")
        _mock_user(monkeypatch)
        resp = _run(_mint_ticket(FakeRequest(body={"return_url": "http://evil.com"}), CFG))
        assert resp.status_code == 400

    def test_mint_without_return_url(self, repo, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_CONNECT_PUBLIC_URL", "http://localhost:8000")
        _mock_user(monkeypatch)
        resp = _run(_mint_ticket(FakeRequest(body={}), CFG))
        data = _body(resp)
        assert "return_url=" not in data["connect_url"]


# ---------------------------------------------------------------------------
# GET /connect/<name>/callback  (full round-trip via mocked token exchange)
# ---------------------------------------------------------------------------


class TestFinishConnect:
    def _common_env(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")
        monkeypatch.setenv("BOND_MCPS_CONNECT_PUBLIC_URL", "http://localhost:8000")
        monkeypatch.setenv("X_CLIENT_ID", "cid")
        monkeypatch.setenv("X_CLIENT_SECRET", "sec")

    def test_callback_success_redirects_and_saves(self, repo, monkeypatch):
        self._common_env(monkeypatch)
        monkeypatch.setattr(
            "auth.connect_routes._exchange_code",
            lambda **kw: {"access_token": "tok", "scope": "read write", "expires_in": 3600},
        )
        _stash_pkce(
            state="st1",
            user_key=USER,
            code_verifier="v",
            provider="atlassian",
            return_url="http://localhost:8000/connections",
        )
        resp = _run(_finish_connect(FakeRequest(query={"code": "c", "state": "st1"}), CFG))
        assert resp.status_code == 302
        assert (
            resp.headers["location"]
            == "http://localhost:8000/connections?connection_success=atlassian"
        )
        saved = TokenRepository().get_token(USER, "atlassian")
        assert saved is not None
        assert saved.get("scopes") == "read write"

    def test_callback_failure_redirects_error(self, repo, monkeypatch):
        self._common_env(monkeypatch)

        def _boom(**kw):
            raise RuntimeError("provider said no")

        monkeypatch.setattr("auth.connect_routes._exchange_code", _boom)
        _stash_pkce(
            state="st2",
            user_key=USER,
            code_verifier="v",
            provider="atlassian",
            return_url="http://localhost:8000/connections",
        )
        resp = _run(_finish_connect(FakeRequest(query={"code": "c", "state": "st2"}), CFG))
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert "connection_error=atlassian" in loc
        assert "error=token_exchange_failed" in loc
