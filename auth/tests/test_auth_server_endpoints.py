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

    def test_rejects_query_fragment_and_userinfo(self, client):
        """The AS appends its own ?code=... to the redirect_uri, so a registered URI that
        already carries a query would fold `code` into the client's last param. A fragment
        is forbidden by RFC 6749 §3.1.2 outright."""
        for bad_uri in (
            "http://127.0.0.1:18999/callback?evil=1",
            "http://127.0.0.1:18999/callback#frag",
            "http://user:pw@127.0.0.1:18999/callback",
        ):
            r = client.post(
                "/oauth/register", json={"client_name": "x", "redirect_uris": [bad_uri]}
            )
            assert r.status_code == 400, bad_uri
            assert r.json()["error"] == "invalid_client_metadata"

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

    def test_authorize_allows_different_loopback_port(self, client, patched_upstream):
        """RFC 8252 §7.3: native apps pick an ephemeral loopback port per auth
        attempt, so a loopback redirect_uri must match a registered loopback URI
        on scheme/host/path even when the port differs."""
        client_id = self._register_client(client)  # registered port 18999
        _, challenge = generate_pkce_pair()
        r = client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:52892/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": "github",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_authorize_loopback_carveout_still_checks_host_and_path(self, client, patched_upstream):
        client_id = self._register_client(client)  # registered 127.0.0.1 /callback
        _, challenge = generate_pkce_pair()
        for bad_uri in (
            "http://localhost:18999/callback",  # host differs from registered
            "http://127.0.0.1:18999/other",  # path differs
            "https://127.0.0.1:18999/callback",  # scheme differs
            "http://attacker.example:18999/callback",  # not loopback at all
            # A smuggled query would corrupt the success redirect, which is built as
            # f"{redirect_uri}?{urlencode(code=...)}" — `code` would parse into `evil`.
            "http://127.0.0.1:52892/callback?evil=1",
            "http://127.0.0.1:52892/callback#frag",
            "http://user:pw@127.0.0.1:52892/callback",  # userinfo is not part of the match
            "http://127.0.0.1:notaport/callback",  # unparseable port must not crash
        ):
            r = client.get(
                "/oauth/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": bad_uri,
                    "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "client-state",
                    "resource": "github",
                },
                follow_redirects=False,
            )
            assert r.status_code == 400, bad_uri
            assert r.json()["error_description"] == "redirect_uri not registered."

    def test_authorize_rejects_stale_query_bearing_registration(self, client, patched_upstream):
        """Registration now rejects a query, but rows predating that check (or seeded via
        BOND_MCPS_STATIC_CLIENTS) may still hold one. Such a URI must not be usable even
        though it matches its registration exactly, or the appended `code` is corrupted."""
        from auth.auth_server import clients as client_registry

        bad = "http://127.0.0.1:18999/callback?evil=1"
        record = client_registry.ClientRecord(
            client_id="bm-stale",
            client_name="stale",
            redirect_uris=[bad],
            token_endpoint_auth_method="none",
            grant_types=list(client_registry.DEFAULT_GRANT_TYPES),
            response_types=list(client_registry.DEFAULT_RESPONSE_TYPES),
            scope=None,
            is_static=False,
        )
        client_registry._persist(record)

        assert client_registry.is_redirect_uri_allowed(record, bad) is False
        _, challenge = generate_pkce_pair()
        r = client.get(
            "/oauth/authorize",
            params={
                "client_id": "bm-stale",
                "redirect_uri": bad,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert r.json()["error_description"] == "redirect_uri not registered."

    def test_authorize_never_redirects_to_unvalidated_uri(self, client, patched_upstream):
        """OAuth 2.1 §4.1.2.1: an unregistered redirect_uri must NEVER receive a redirect,
        or the AS is an open redirector usable for phishing off a trusted origin. Every
        pre-validation failure has to answer with a direct 400 instead."""
        evil = "https://evil.example.com/steal"
        for params in (
            # Missing code_challenge — previously error-redirected to `evil` before the
            # redirect_uri was ever checked.
            {"client_id": "whatever", "redirect_uri": evil, "response_type": "code"},
            {"client_id": "whatever", "redirect_uri": evil, "response_type": "token"},
            # Unknown client, bad method: still must not bounce off evil.
            {
                "client_id": "unknown",
                "redirect_uri": evil,
                "response_type": "code",
                "code_challenge": "x",
                "code_challenge_method": "plain",
            },
        ):
            r = client.get(
                "/oauth/authorize", params={**params, "state": "s"}, follow_redirects=False
            )
            assert r.status_code == 400, params
            assert "evil.example.com" not in r.headers.get("location", "")

    def test_authorize_reports_late_errors_via_redirect(self, client, patched_upstream):
        """The flip side: once redirect_uri IS validated, protocol errors are reported to
        the client by redirect (per spec) rather than a 400."""
        client_id = self._register_client(client)
        r = client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:18999/callback",
                "response_type": "token",  # unsupported => error redirect
                "code_challenge": "x",
                "state": "s",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("http://127.0.0.1:18999/callback?")
        assert "error=unsupported_response_type" in loc

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
