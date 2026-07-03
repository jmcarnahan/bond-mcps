"""Tests for the RFC 8693 token-exchange grant on the Authorization Server.

Mirrors the fixture style of ``test_auth_server_endpoints.py`` (SQLite +
env-injected RS256 signing key). The delegated grant maps bond-ai's HS256
"bond JWT" to an AS-issued RS256 access token; ``cognito_lookup.resolve_
cognito_sub`` is mocked so no AWS calls happen.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from auth.auth_server.token_exchange import GRANT_TOKEN_EXCHANGE
from auth.db import reset_for_tests

BOND_JWT_SECRET = "shared-hs256-secret-value-at-least-32-bytes-long"
RESOURCE = "github"
COGNITO_SUB = "cognito-sub-xyz"


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
def base_env(signing_pem, tmp_path, monkeypatch):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    monkeypatch.setenv("BOND_MCPS_AS_PRIVATE_KEY_PEM", signing_pem)
    monkeypatch.setenv("BOND_MCPS_AS_BASE_URL", "http://localhost:8001")
    monkeypatch.setenv("BOND_MCPS_AS_ENABLED", "1")
    # No Cognito pool: resolve_cognito_sub is mocked in every test that mints.
    monkeypatch.delenv("BOND_MCPS_AS_COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.delenv("BOND_MCPS_AS_EXCHANGE_TOKEN_TTL_SECONDS", raising=False)
    monkeypatch.delenv("BOND_MCPS_AS_EXCHANGE_CLIENT_ID", raising=False)
    reset_for_tests()
    yield monkeypatch
    reset_for_tests()


@pytest.fixture
def enabled_env(base_env):
    base_env.setenv("BOND_MCPS_AS_BOND_JWT_SECRET", BOND_JWT_SECRET)
    return base_env


@pytest.fixture
def client(enabled_env):
    from auth.auth_server import build_app

    return TestClient(build_app())


@pytest.fixture
def resolve_sub():
    with patch(
        "auth.auth_server.token_exchange.cognito_lookup.resolve_cognito_sub",
        return_value=COGNITO_SUB,
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mint_bond_jwt(
    *,
    secret: str = BOND_JWT_SECRET,
    sub: str = "Alice@Example.com",
    aud=None,
    iss: str = "bond-ai",
    exp_delta: int = 300,
    include_exp: bool = True,
) -> str:
    now = int(time.time())
    payload = {
        "iss": iss,
        "sub": sub,
        "aud": aud if aud is not None else ["bond-ai-api", "mcp-server"],
        "iat": now,
    }
    if include_exp:
        payload["exp"] = now + exp_delta
    return jwt.encode(payload, secret, algorithm="HS256")


def _post_exchange(client, subject_token, **overrides):
    data = {
        "grant_type": GRANT_TOKEN_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "resource": RESOURCE,
        "client_id": "bond-ai",
    }
    data.update(overrides)
    # None means "omit this field".
    data = {k: v for k, v in data.items() if v is not None}
    return client.post("/oauth/token", data=data)


def _decode_access_token(client, token):
    jwks = client.get("/.well-known/jwks.json").json()
    keyset = jwt.PyJWKSet.from_dict(jwks)
    header = jwt.get_unverified_header(token)
    signing_key = next(k for k in keyset.keys if k.key_id == header["kid"])
    return jwt.decode(
        token,
        key=signing_key.key,
        algorithms=["RS256"],
        audience=RESOURCE,
        issuer="http://localhost:8001",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_exchange_success(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
        assert body["token_type"] == "Bearer"
        assert abs(body["expires_in"] - 300) <= 1
        assert "refresh_token" not in body

        payload = _decode_access_token(client, body["access_token"])
        assert payload["sub"] == COGNITO_SUB
        assert payload["aud"] == RESOURCE
        assert payload["client_id"] == "bond-ai"
        assert payload["email"] == "alice@example.com"
        # Email passed to the resolver is normalized lowercase.
        resolve_sub.assert_called_once_with("alice@example.com")

    def test_accepts_bare_mcp_server_audience(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(aud=["mcp-server"]))
        assert r.status_code == 200, r.text

    def test_accepts_two_element_audience(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(aud=["bond-ai-api", "mcp-server"]))
        assert r.status_code == 200, r.text

    def test_subject_token_type_optional(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(), subject_token_type=None)
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


class TestRejections:
    def test_wrong_secret(self, client, resolve_sub):
        r = _post_exchange(
            client, _mint_bond_jwt(secret="a-different-secret-at-least-32-bytes-long")
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_expired(self, client, resolve_sub):
        # Beyond the 30s leeway.
        r = _post_exchange(client, _mint_bond_jwt(exp_delta=-120))
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_missing_exp(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(include_exp=False))
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_wrong_issuer(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(iss="evil"))
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_wrong_audience(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(aud=["some-other-api"]))
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_missing_resource(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(), resource=None)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_target"

    def test_missing_subject_token(self, client, resolve_sub):
        r = client.post(
            "/oauth/token",
            data={
                "grant_type": GRANT_TOKEN_EXCHANGE,
                "resource": RESOURCE,
                "client_id": "bond-ai",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_wrong_subject_token_type(self, client, resolve_sub):
        r = _post_exchange(
            client,
            _mint_bond_jwt(),
            subject_token_type="urn:ietf:params:oauth:token-type:saml2",
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_wrong_client_id(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(), client_id="not-bond-ai")
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client"

    def test_missing_client_id(self, client, resolve_sub):
        r = _post_exchange(client, _mint_bond_jwt(), client_id=None)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client"

    def test_unresolvable_subject(self, client):
        with patch(
            "auth.auth_server.token_exchange.cognito_lookup.resolve_cognito_sub",
            return_value=None,
        ):
            r = _post_exchange(client, _mint_bond_jwt())
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Feature flag (secret unset)
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_grant_unsupported_when_secret_unset(self, base_env):
        from auth.auth_server import build_app

        c = TestClient(build_app())
        r = _post_exchange(c, _mint_bond_jwt())
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"

    def test_metadata_hides_grant_when_disabled(self, base_env):
        from auth.auth_server import build_app

        c = TestClient(build_app())
        body = c.get("/.well-known/oauth-authorization-server").json()
        assert GRANT_TOKEN_EXCHANGE not in body["grant_types_supported"]

    def test_metadata_advertises_grant_when_enabled(self, client):
        body = client.get("/.well-known/oauth-authorization-server").json()
        assert GRANT_TOKEN_EXCHANGE in body["grant_types_supported"]


# ---------------------------------------------------------------------------
# TTL clamp
# ---------------------------------------------------------------------------


class TestTtlClamp:
    def test_ttl_floor(self, client, resolve_sub, enabled_env):
        enabled_env.setenv("BOND_MCPS_AS_EXCHANGE_TOKEN_TTL_SECONDS", "10")
        r = _post_exchange(client, _mint_bond_jwt())
        assert r.status_code == 200
        assert r.json()["expires_in"] == 60

    def test_ttl_ceiling(self, client, resolve_sub, enabled_env):
        enabled_env.setenv("BOND_MCPS_AS_EXCHANGE_TOKEN_TTL_SECONDS", "99999")
        r = _post_exchange(client, _mint_bond_jwt())
        assert r.status_code == 200
        assert r.json()["expires_in"] == 3600
