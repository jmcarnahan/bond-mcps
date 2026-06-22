"""Live integration test for the /connect routes — the real proof point.

Unlike test_connect_routes.py (which calls handlers directly with a mocked
get_access_token), this builds a real FastMCP server with the HS256
RemoteAuthProvider, registers the connect routes, and drives them over HTTP
through the actual auth middleware. It proves the load-bearing assumptions the
delegation design depends on:

  * a forwarded valid Bearer is validated and surfaced via get_access_token
    (so status/disconnect resolve the user_key),
  * a missing OR invalid Bearer yields 401 on the JWT-gated routes,
  * the browser-facing start route is reachable WITHOUT a Bearer (the consent
    redirect carries a ticket, not a JWT).
"""

import time

import jwt
import pytest
from starlette.testclient import TestClient

from auth.connect_routes import ProviderConnectConfig, register_connect_routes
from auth.db.repository import TokenRepository

SECRET = "shared-secret-xyz-at-least-32-bytes-long-000"  # noqa: S105 - test-only
USER = "alice@example.com"

CFG = ProviderConnectConfig(
    name="atlassian",
    authorize_url="https://auth.example/authorize",
    token_url="https://auth.example/token",
    scopes="read write",
    client_id_env="X_CLIENT_ID",
    client_secret_env="X_CLIENT_SECRET",
)


def _token(*, secret=SECRET, iss="bond-ai", aud=None, exp_delta=3600, sub=USER):
    return jwt.encode(
        {
            "sub": sub,
            "iss": iss,
            "aud": ["bond-ai-api", "mcp-server"] if aud is None else aud,
            "exp": int(time.time()) + exp_delta,
        },
        secret,
        algorithm="HS256",
    )


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(repo, monkeypatch):
    """A TestClient over a real FastMCP app with HS256 delegation auth + connect routes.

    ``repo`` brings the in-process DB + encryption so status/disconnect work.
    """
    monkeypatch.setenv("BOND_MCPS_JWT_PUBLIC_KEY", SECRET)
    monkeypatch.setenv("BOND_MCPS_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("BOND_MCPS_JWT_ISSUER", "bond-ai")
    monkeypatch.setenv("BOND_MCPS_JWT_AUDIENCE", "mcp-server")
    monkeypatch.setenv("BOND_MCPS_AS_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("BOND_MCPS_PUBLIC_URL", "http://localhost:18099")
    monkeypatch.setenv("BOND_MCPS_CONNECT_PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv("BOND_MCPS_ALLOWED_RETURN_HOSTS", "localhost")

    from fastmcp import FastMCP

    from auth.jwt_identity import build_remote_auth_provider

    mcp = FastMCP("connect-it", auth=build_remote_auth_provider("atlassian"))
    register_connect_routes(mcp, CFG)
    return TestClient(mcp.http_app())


class TestConnectRoutesLive:
    def test_status_with_valid_bearer_resolves_user(self, client):
        # No token stored yet for this user.
        resp = client.get("/connect/atlassian/status", headers=_bearer(_token()))
        assert resp.status_code == 200
        assert resp.json() == {"connected": False, "valid": True, "scopes": None}

    def test_status_reflects_stored_token(self, client):
        TokenRepository().save_token(
            USER, "atlassian", {"access_token": "t", "scopes": "read write", "refresh_token": "r"}
        )
        resp = client.get("/connect/atlassian/status", headers=_bearer(_token()))
        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is True
        assert body["scopes"] == "read write"

    def test_status_without_bearer_is_401(self, client):
        assert client.get("/connect/atlassian/status").status_code == 401

    def test_status_with_invalid_bearer_is_401(self, client):
        bad = _token(secret="wrong-secret-wrong-secret-wrong-secret")
        assert client.get("/connect/atlassian/status", headers=_bearer(bad)).status_code == 401

    def test_disconnect_with_valid_bearer(self, client):
        TokenRepository().save_token(USER, "atlassian", {"access_token": "t"})
        resp = client.request("DELETE", "/connect/atlassian", headers=_bearer(_token()))
        assert resp.status_code == 200
        assert resp.json() == {"disconnected": True}
        assert TokenRepository().get_token(USER, "atlassian") is None

    def test_start_route_is_reachable_without_bearer(self, client):
        # Browser hits /connect/<name> with a ticket, NOT a JWT. Missing ticket
        # must yield 400 (route ran), proving the route is not JWT-gated (not 401).
        resp = client.get("/connect/atlassian")
        assert resp.status_code == 400
