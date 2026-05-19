"""End-to-end tests for the bond-mcps Authorization Server.

Exercises the full ``/oauth/register`` → ``/oauth/authorize`` →
``/oauth/upstream/callback`` → ``/oauth/token`` flow against a stubbed
OIDC upstream IdP and an in-memory SQLite. Also covers the discovery
endpoints and basic DCR validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from auth.alembic_config import upgrade_head
from auth.db import reset_for_tests
from auth.oauth_utils import generate_pkce_pair

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def signing_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def env(signing_pem, tmp_path, monkeypatch):
    # Token encryption key — required because the AS encrypts pending-auth
    # rows (PKCE code_verifier, upstream state) before persisting. Without
    # this set, `encryption.encrypt()` raises TokenEncryptionError.
    from auth import encryption

    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", encryption.generate_key())
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY_FILE", raising=False)

    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    monkeypatch.setenv("BOND_MCPS_AS_PRIVATE_KEY_PEM", signing_pem)
    monkeypatch.setenv("BOND_MCPS_AS_BASE_URL", "http://localhost:8001")
    monkeypatch.setenv("BOND_MCPS_AS_ENABLED", "1")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_IDP", "okta")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_ISSUER", "https://example.okta.com")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_ID", "upstream-cid")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv(
        "BOND_MCPS_UPSTREAM_REDIRECT_URI", "http://localhost:8001/oauth/upstream/callback"
    )
    reset_for_tests()
    upgrade_head()
    yield
    reset_for_tests()


@pytest.fixture
def client(env):
    from auth.auth_server import build_app

    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Upstream IdP stub
# ---------------------------------------------------------------------------


@dataclass
class StubUpstream:
    """Returns deterministic auth URLs + user info; remembers the last code_verifier."""

    last_state: str | None = None
    user_sub: str = "alice-sub"
    user_email: str = "alice@example.com"

    def authorize_url(self, *, state, code_challenge, code_challenge_method="S256"):
        self.last_state = state
        return f"https://example.okta.com/authorize?state={state}&code_challenge={code_challenge}"

    def exchange_code(self, *, code, code_verifier):
        from auth.auth_server.upstream import UpstreamUserInfo

        return UpstreamUserInfo(
            sub=self.user_sub, email=self.user_email, name="Alice", raw_claims={}
        )


@pytest.fixture
def stub_upstream():
    return StubUpstream()


@pytest.fixture
def patched_upstream(stub_upstream):
    with patch("auth.auth_server.endpoints.get_upstream_idp", return_value=stub_upstream):
        yield stub_upstream


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_authorization_server_metadata(self, client):
        r = client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        body = r.json()
        assert body["issuer"] == "http://localhost:8001"
        assert body["authorization_endpoint"].endswith("/oauth/authorize")
        assert body["token_endpoint"].endswith("/oauth/token")
        assert body["jwks_uri"].endswith("/.well-known/jwks.json")
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_code" in body["grant_types_supported"]

    def test_jwks(self, client):
        r = client.get("/.well-known/jwks.json")
        assert r.status_code == 200
        body = r.json()
        assert len(body["keys"]) == 1
        jwk = body["keys"][0]
        assert jwk["kty"] == "RSA" and jwk["alg"] == "RS256"


# ---------------------------------------------------------------------------
# Dynamic Client Registration
# ---------------------------------------------------------------------------


class TestDCR:
    def test_registers_public_client(self, client):
        r = client.post(
            "/oauth/register",
            json={
                "client_name": "claude-code",
                "redirect_uris": ["http://127.0.0.1:18999/callback"],
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["client_id"].startswith("bm-")
        assert body["token_endpoint_auth_method"] == "none"
        assert "authorization_code" in body["grant_types"]

    def test_rejects_missing_redirect_uris(self, client):
        r = client.post("/oauth/register", json={"client_name": "x"})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client_metadata"

    def test_rejects_non_loopback_http(self, client):
        r = client.post(
            "/oauth/register",
            json={
                "client_name": "x",
                "redirect_uris": ["http://attacker.example.com/cb"],
            },
        )
        assert r.status_code == 400

    def test_rejects_non_json_body(self, client):
        r = client.post("/oauth/register", content=b"not-json")
        assert r.status_code == 400

    def test_rejects_https_when_allowlist_unset(self, client):
        """Fail-closed: HTTPS callback rejected when allowlist env is unset.

        Without this default, a deployment could be tricked into issuing
        JWTs to arbitrary attacker-controlled hosts via DCR.
        """
        r = client.post(
            "/oauth/register",
            json={
                "client_name": "evil",
                "redirect_uris": ["https://attacker.example/cb"],
            },
        )
        assert r.status_code == 400
        assert "ALLOWED_REDIRECT_HOSTS" in r.json()["error_description"]

    def test_accepts_https_when_host_in_allowlist(self, client, monkeypatch):
        monkeypatch.setenv(
            "BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS",
            "claude.workstation.example,other.example",
        )
        r = client.post(
            "/oauth/register",
            json={
                "client_name": "ok",
                "redirect_uris": ["https://claude.workstation.example/cb"],
            },
        )
        assert r.status_code == 201

    def test_rejects_https_when_host_not_in_allowlist(self, client, monkeypatch):
        monkeypatch.setenv(
            "BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS",
            "trusted.example",
        )
        r = client.post(
            "/oauth/register",
            json={
                "client_name": "evil",
                "redirect_uris": ["https://untrusted.example/cb"],
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Authorize → token full round-trip
# ---------------------------------------------------------------------------


class TestFullFlow:
    def _register_client(self, client) -> str:
        r = client.post(
            "/oauth/register",
            json={
                "client_name": "claude-code",
                "redirect_uris": ["http://127.0.0.1:18999/callback"],
            },
        )
        assert r.status_code == 201
        return r.json()["client_id"]

    def test_authorize_redirects_to_upstream(self, client, patched_upstream):
        client_id = self._register_client(client)
        verifier, challenge = generate_pkce_pair()
        r = client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": "github",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://example.okta.com/authorize?")
        # The bond_state set on the upstream is non-empty.
        assert patched_upstream.last_state

    def test_full_round_trip(self, client, patched_upstream):
        client_id = self._register_client(client)
        verifier, challenge = generate_pkce_pair()
        # 1. Authorize → upstream redirect
        r = client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": "github",
            },
            follow_redirects=False,
        )
        bond_state = patched_upstream.last_state

        # 2. Simulate upstream callback
        r = client.get(
            "/oauth/upstream/callback",
            params={"code": "upstream-code", "state": bond_state},
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("http://127.0.0.1:18999/callback?")
        qs = parse_qs(urlsplit(loc).query)
        assert qs["state"] == ["client-state"]
        auth_code = qs["code"][0]

        # 3. Token exchange
        r = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert r.status_code == 200
        body = r.json()
        token = body["access_token"]

        # 4. JWT validates against the JWKS we publish
        jwks = client.get("/.well-known/jwks.json").json()
        # Use PyJWT's PyJWKClient against an in-memory JWKS dict.
        keyset = jwt.PyJWKSet.from_dict(jwks)
        header = jwt.get_unverified_header(token)
        signing_key = next(k for k in keyset.keys if k.key_id == header["kid"])
        payload = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=["RS256"],
            audience="github",
            issuer="http://localhost:8001",
        )
        assert payload["sub"] == "alice-sub"
        assert payload["email"] == "alice@example.com"
        assert payload["client_id"] == client_id

    def test_token_endpoint_rejects_replay(self, client, patched_upstream):
        """Authorization codes are single-use."""
        client_id = self._register_client(client)
        verifier, challenge = generate_pkce_pair()
        client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "x",
            },
            follow_redirects=False,
        )
        bond_state = patched_upstream.last_state
        callback = client.get(
            "/oauth/upstream/callback",
            params={"code": "u-code", "state": bond_state},
            follow_redirects=False,
        )
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:18999/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        }
        ok = client.post("/oauth/token", data=params)
        assert ok.status_code == 200
        replay = client.post("/oauth/token", data=params)
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

    def test_token_endpoint_rejects_wrong_pkce(self, client, patched_upstream):
        client_id = self._register_client(client)
        verifier, challenge = generate_pkce_pair()
        client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "x",
            },
            follow_redirects=False,
        )
        bond_state = patched_upstream.last_state
        callback = client.get(
            "/oauth/upstream/callback",
            params={"code": "u-code", "state": bond_state},
            follow_redirects=False,
        )
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        r = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "client_id": client_id,
                "code_verifier": verifier + "tampered",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


class TestAuthorizeRejection:
    def test_unknown_client_id(self, client, patched_upstream):
        r = client.get(
            "/oauth/authorize",
            params={
                "client_id": "unknown",
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "response_type": "code",
                "code_challenge": "x",
                "state": "s",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client"

    def test_unregistered_redirect_uri(self, client, patched_upstream):
        # Register a client with redirect A; then try to authorize using redirect B.
        r = client.post(
            "/oauth/register",
            json={
                "client_name": "x",
                "redirect_uris": ["http://127.0.0.1:18999/callback"],
            },
        )
        cid = r.json()["client_id"]
        r = client.get(
            "/oauth/authorize",
            params={
                "client_id": cid,
                "redirect_uri": "http://127.0.0.1:99999/other",
                "response_type": "code",
                "code_challenge": "x",
                "code_challenge_method": "S256",
                "state": "s",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"
