"""End-to-end OAuth test — synthetic Claude Code against the real AS + MCP wiring.

This is the test that catches *audience drift* between FastMCP's PRM
``resource`` URI and the JWT verifier's expected audience — the single most
likely cause of "everything looks right but every request 401s" in
production. It walks the exact sequence the MCP spec mandates:

  1. Fetch the MCP's protected-resource-metadata document (RFC 9728).
  2. Fetch the AS's authorization-server metadata (RFC 8414).
  3. Register a public client via RFC 7591 Dynamic Client Registration.
  4. Run the PKCE authorize → upstream-callback → token exchange flow.
  5. Take the issued JWT and run it through the same JWTVerifier the MCP
     uses, asserting it accepts the token without any operator-level env
     tweak beyond ``BOND_MCPS_PUBLIC_URL``.

The upstream OIDC IdP is stubbed; the AS and MCP are both real
in-process Starlette/FastMCP apps wired via ``starlette.testclient``.

If FastMCP's PRM URL convention changes (today it's ``<base_url>/mcp``),
or our audience defaulting drifts, this test will fail and tell you
exactly where the chain broke.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from auth.alembic_config import upgrade_head
from auth.db import reset_for_tests
from auth.oauth_utils import generate_pkce_pair

MCP_PUBLIC_URL = "http://github-mcp.test"
AS_BASE_URL = "http://localhost:8001"
CALLBACK_PORT = 18999
CLIENT_REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


@pytest.fixture
def env(keypair, tmp_path, monkeypatch):
    """Set both AS-side and MCP-side env. Deliberately omits
    BOND_MCPS_JWT_AUDIENCE — the audience must default to the canonical
    PRM URI per RFC 8707, and the test verifies that default works.
    """
    priv_pem, pub_pem = keypair
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    # AS
    monkeypatch.setenv("BOND_MCPS_AS_ENABLED", "1")
    monkeypatch.setenv("BOND_MCPS_AS_BASE_URL", AS_BASE_URL)
    monkeypatch.setenv("BOND_MCPS_AS_PRIVATE_KEY_PEM", priv_pem)
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_IDP", "okta")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_ISSUER", "https://example.okta.com")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_ID", "upstream-cid")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv(
        "BOND_MCPS_UPSTREAM_REDIRECT_URI",
        f"{AS_BASE_URL}/oauth/upstream/callback",
    )
    # MCP
    monkeypatch.setenv("BOND_MCPS_JWT_PUBLIC_KEY", pub_pem)
    monkeypatch.setenv("BOND_MCPS_JWT_ALGORITHM", "RS256")
    monkeypatch.setenv("BOND_MCPS_JWT_ISSUER", AS_BASE_URL)
    monkeypatch.setenv("BOND_MCPS_PUBLIC_URL", MCP_PUBLIC_URL)
    monkeypatch.delenv("BOND_MCPS_JWT_AUDIENCE", raising=False)
    reset_for_tests()
    upgrade_head()
    yield
    reset_for_tests()


@dataclass
class _StubUpstream:
    last_state: str | None = None
    user_sub: str = "alice-sub"
    user_email: str = "alice@example.com"

    def authorize_url(self, *, state, code_challenge, code_challenge_method="S256"):
        self.last_state = state
        return f"https://example.okta.com/authorize?state={state}"

    def exchange_code(self, *, code, code_verifier):
        from auth.auth_server.upstream import UpstreamUserInfo

        return UpstreamUserInfo(
            sub=self.user_sub, email=self.user_email, name="Alice", raw_claims={}
        )


@pytest.fixture
def stub_upstream():
    return _StubUpstream()


# ---------------------------------------------------------------------------
# The owl test
# ---------------------------------------------------------------------------


def test_synthetic_claude_code_full_oauth_flow(env, stub_upstream):
    from fastmcp import FastMCP

    from auth.auth_server import build_app
    from auth.jwt_identity import build_remote_auth_provider, build_verifier

    # === Build both apps with the production wiring ============================
    as_app = build_app()

    provider = build_remote_auth_provider("github")
    assert provider is not None, "JWT mode should be on with the configured env"

    mcp = FastMCP("test-github", auth=provider)

    @mcp.tool()
    def echo() -> str:
        return "ok"

    mcp_app = mcp.http_app()

    as_client = TestClient(as_app)
    mcp_client = TestClient(mcp_app)

    # === Step 1: Synthetic Claude Code reads protected-resource-metadata ====
    # (We skip the actual POST /mcp 401 dance because FastMCP's
    # StreamableHTTP transport requires a lifespan that TestClient doesn't
    # set up. The PRM document is the part of discovery Claude Code uses.)
    r = mcp_client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200, r.text
    prm = r.json()

    resource_uri = prm["resource"].rstrip("/")
    # PRM resource must equal <public_url>/mcp per FastMCP's convention.
    # This is the URI Claude Code will pass as RFC 8707 `resource`.
    assert resource_uri == f"{MCP_PUBLIC_URL}/mcp", (
        f"PRM resource URI drifted: got {resource_uri!r}, expected "
        f"{MCP_PUBLIC_URL!r}/mcp. The verifier's audience defaulting needs "
        "to be updated to match."
    )

    auth_servers = [s.rstrip("/") for s in prm["authorization_servers"]]
    assert AS_BASE_URL.rstrip("/") in auth_servers

    # === Step 2: Fetch AS metadata — advertises both grant types ============
    r = as_client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    as_meta = r.json()
    assert "authorization_code" in as_meta["grant_types_supported"]
    assert "refresh_token" in as_meta["grant_types_supported"]
    assert as_meta["code_challenge_methods_supported"] == ["S256"]
    assert as_meta["token_endpoint"].endswith("/oauth/token")

    # === Step 3: DCR ========================================================
    r = as_client.post(
        "/oauth/register",
        json={
            "client_name": "synthetic-claude-code",
            "redirect_uris": [CLIENT_REDIRECT_URI],
        },
    )
    assert r.status_code == 201, r.text
    dcr = r.json()
    client_id = dcr["client_id"]
    assert "refresh_token" in dcr["grant_types"]

    # === Step 4: PKCE round-trip ===========================================
    code_verifier, code_challenge = generate_pkce_pair()

    with patch(
        "auth.auth_server.endpoints.get_upstream_idp",
        return_value=stub_upstream,
    ):
        r = as_client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": resource_uri,
            },
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text
        bond_state = stub_upstream.last_state
        assert bond_state, "Upstream IdP wasn't given a state to redirect with"

        # Simulate upstream returning successfully
        r = as_client.get(
            "/oauth/upstream/callback",
            params={"code": "upstream-code", "state": bond_state},
            follow_redirects=False,
        )
        assert r.status_code == 302
        callback_url = r.headers["location"]
        assert callback_url.startswith(CLIENT_REDIRECT_URI)
        qs = parse_qs(urlsplit(callback_url).query)
        assert qs["state"] == ["client-state"]
        auth_code = qs["code"][0]

        # Token exchange
        r = as_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert r.status_code == 200, r.text
        token_body = r.json()
        access_token = token_body["access_token"]
        refresh_token = token_body.get("refresh_token")
        assert refresh_token, "AS must issue a refresh_token alongside access_token"

    # === Step 5: Run the JWT through the MCP's verifier (the bug catcher) ===
    # This is the production check: would FastMCP's middleware accept the
    # token the AS issued? Audience drift would manifest as None here.
    verifier = build_verifier()
    access_obj = asyncio.run(verifier.verify_token(access_token))
    assert access_obj is not None, (
        "JWTVerifier rejected an AS-issued token. The most likely cause is "
        "audience drift: the JWT's `aud` claim (set from RFC 8707 `resource`) "
        "doesn't match what the verifier expects. Confirm "
        "_resolve_audience() in auth/jwt_identity.py picks up "
        "BOND_MCPS_PUBLIC_URL when BOND_MCPS_JWT_AUDIENCE is unset."
    )
    assert access_obj.claims["sub"] == stub_upstream.user_sub
    assert access_obj.claims["email"] == stub_upstream.user_email
    assert access_obj.claims["client_id"] == client_id
    # Audience must be the canonical PRM URI (RFC 8707).
    assert access_obj.claims["aud"] == resource_uri
    # Issuer must match the AS.
    assert access_obj.claims["iss"] == AS_BASE_URL


def test_audience_default_matches_prm_resource_url(env):
    """Specific guard for CRITICAL-1: when BOND_MCPS_JWT_AUDIENCE is unset
    and BOND_MCPS_PUBLIC_URL is set, the verifier accepts an audience of
    ``<public_url>/mcp`` (the exact value FastMCP advertises in PRM and the
    exact value Claude Code will pass as RFC 8707 `resource`).
    """
    from auth.jwt_identity import build_verifier

    verifier = build_verifier()
    aud = getattr(verifier, "audience", None)
    if isinstance(aud, list):
        assert f"{MCP_PUBLIC_URL}/mcp" in aud
    else:
        assert aud == f"{MCP_PUBLIC_URL}/mcp"


def test_audience_env_override_still_works(env, monkeypatch, keypair):
    """Operators with a friendly audience name (e.g. ``github``) must keep
    working — the canonical URI gets merged in alongside, not replaced."""
    monkeypatch.setenv("BOND_MCPS_JWT_AUDIENCE", "github")

    from auth.jwt_identity import build_verifier

    verifier = build_verifier()
    aud = verifier.audience
    assert isinstance(aud, list), f"expected a list, got {aud!r}"
    assert "github" in aud
    assert f"{MCP_PUBLIC_URL}/mcp" in aud


def test_authorization_server_metadata_advertises_refresh_token(env):
    """AS advertises both grants per RFC 8414 §2."""
    from auth.auth_server import build_app

    client = TestClient(build_app())
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


def test_dcr_grants_refresh_token(env):
    """Public clients registered via DCR get the refresh_token grant by
    default. Without it, Claude Code prompts for re-auth every hour."""
    from auth.auth_server import build_app

    client = TestClient(build_app())
    r = client.post(
        "/oauth/register",
        json={
            "client_name": "x",
            "redirect_uris": ["http://127.0.0.1:1/callback"],
        },
    )
    assert r.status_code == 201
    assert "refresh_token" in r.json()["grant_types"]


def test_refresh_token_round_trip(env, stub_upstream):
    """Full discovery + DCR + code grant + refresh_token grant round-trip.

    After the access_token expires, Claude Code presents the refresh_token
    and gets a fresh access_token (and a rotated refresh_token) without
    bouncing the user back through Cognito.
    """
    import asyncio

    from auth.auth_server import build_app
    from auth.jwt_identity import build_verifier

    as_client = TestClient(build_app())

    # DCR + initial code grant (compressed since the full flow is covered
    # by test_synthetic_claude_code_full_oauth_flow)
    cid = as_client.post(
        "/oauth/register",
        json={
            "client_name": "refresh-test",
            "redirect_uris": [CLIENT_REDIRECT_URI],
        },
    ).json()["client_id"]
    code_verifier, code_challenge = generate_pkce_pair()
    with patch(
        "auth.auth_server.endpoints.get_upstream_idp",
        return_value=stub_upstream,
    ):
        as_client.get(
            "/oauth/authorize",
            params={
                "client_id": cid,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "x",
                "resource": f"{MCP_PUBLIC_URL}/mcp",
            },
            follow_redirects=False,
        )
        callback = as_client.get(
            "/oauth/upstream/callback",
            params={"code": "u", "state": stub_upstream.last_state},
            follow_redirects=False,
        )
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        first_response = as_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "client_id": cid,
                "code_verifier": code_verifier,
            },
        )
        assert first_response.status_code == 200
        first_body = first_response.json()
        first_access = first_body["access_token"]
        first_refresh = first_body["refresh_token"]

    # Step: exchange the refresh_token for a fresh pair (Claude Code's path
    # when the access_token expires)
    second_response = as_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first_refresh,
            "client_id": cid,
        },
    )
    assert second_response.status_code == 200, second_response.text
    second_body = second_response.json()
    new_access = second_body["access_token"]
    new_refresh = second_body["refresh_token"]
    # Rotation: the new refresh_token must differ from the old.
    assert new_refresh != first_refresh
    # The new access_token must validate against the verifier just like
    # the original one did.
    access_obj = asyncio.run(build_verifier().verify_token(new_access))
    assert access_obj is not None
    assert access_obj.claims["sub"] == stub_upstream.user_sub
    assert access_obj.claims["aud"] == f"{MCP_PUBLIC_URL}/mcp"
    # The old refresh_token must be rejected on replay (rotation).
    replay = as_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first_refresh,
            "client_id": cid,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_access_token_ttl_honors_env_override(env, monkeypatch, stub_upstream):
    """Operators can dial ACCESS_TOKEN_TTL_SECONDS via env without a rebuild."""
    monkeypatch.setenv("BOND_MCPS_AS_ACCESS_TOKEN_TTL_SECONDS", "3600")

    from auth.auth_server import build_app

    as_client = TestClient(build_app())
    cid = as_client.post(
        "/oauth/register",
        json={"client_name": "ttl", "redirect_uris": [CLIENT_REDIRECT_URI]},
    ).json()["client_id"]
    code_verifier, code_challenge = generate_pkce_pair()
    with patch(
        "auth.auth_server.endpoints.get_upstream_idp",
        return_value=stub_upstream,
    ):
        as_client.get(
            "/oauth/authorize",
            params={
                "client_id": cid,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "x",
                "resource": f"{MCP_PUBLIC_URL}/mcp",
            },
            follow_redirects=False,
        )
        callback = as_client.get(
            "/oauth/upstream/callback",
            params={"code": "u", "state": stub_upstream.last_state},
            follow_redirects=False,
        )
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        token_body = as_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "client_id": cid,
                "code_verifier": code_verifier,
            },
        ).json()
    assert token_body["expires_in"] == 3600


def test_access_token_ttl_clamps_to_safe_range(env, monkeypatch):
    """Garbage env values fall back to the default; ridiculously large
    values are clamped to refresh-token TTL ceiling so we don't issue a
    JWT that outlives its rotation key."""
    from auth.auth_server.endpoints import _access_token_ttl_seconds

    monkeypatch.setenv("BOND_MCPS_AS_ACCESS_TOKEN_TTL_SECONDS", "not-a-number")
    assert _access_token_ttl_seconds() == 86400

    monkeypatch.setenv("BOND_MCPS_AS_ACCESS_TOKEN_TTL_SECONDS", "1")
    assert _access_token_ttl_seconds() == 60  # min clamp

    monkeypatch.setenv("BOND_MCPS_AS_ACCESS_TOKEN_TTL_SECONDS", str(10**9))
    assert _access_token_ttl_seconds() == 30 * 24 * 3600  # max clamp


def test_refresh_token_rejects_wrong_client_id(env, stub_upstream):
    """A refresh token issued to client A cannot be redeemed by client B."""
    from auth.auth_server import build_app

    as_client = TestClient(build_app())

    cid_a = as_client.post(
        "/oauth/register",
        json={"client_name": "a", "redirect_uris": [CLIENT_REDIRECT_URI]},
    ).json()["client_id"]
    cid_b = as_client.post(
        "/oauth/register",
        json={"client_name": "b", "redirect_uris": [CLIENT_REDIRECT_URI]},
    ).json()["client_id"]

    code_verifier, code_challenge = generate_pkce_pair()
    with patch(
        "auth.auth_server.endpoints.get_upstream_idp",
        return_value=stub_upstream,
    ):
        as_client.get(
            "/oauth/authorize",
            params={
                "client_id": cid_a,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "x",
                "resource": f"{MCP_PUBLIC_URL}/mcp",
            },
            follow_redirects=False,
        )
        callback = as_client.get(
            "/oauth/upstream/callback",
            params={"code": "u", "state": stub_upstream.last_state},
            follow_redirects=False,
        )
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        token_body = as_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLIENT_REDIRECT_URI,
                "client_id": cid_a,
                "code_verifier": code_verifier,
            },
        ).json()

    bad = as_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_body["refresh_token"],
            "client_id": cid_b,
        },
    )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_grant"
