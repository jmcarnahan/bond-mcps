"""HS256 shared-secret JWT verification (the bond-ai delegation mode).

bond-ai issues Bond JWTs (HS256, secret = bond-ai JWT_SECRET_KEY,
iss="bond-ai", aud=["bond-ai-api","mcp-server"]). bond-mcps validates them via
``build_verifier`` configured from ``BOND_MCPS_JWT_PUBLIC_KEY`` (the shared
secret) + ``BOND_MCPS_JWT_ALGORITHM=HS256``. These tests pin accept/reject
semantics so the delegation handshake can't silently break.
"""

import asyncio
import time

import jwt
import pytest

from auth.jwt_identity import build_verifier

SECRET = "shared-secret-xyz-at-least-32-bytes-long-000"  # noqa: S105 - test-only


@pytest.fixture(autouse=True)
def _hs256_env(monkeypatch):
    for k in (
        "BOND_MCPS_JWT_JWKS_URI",
        "BOND_MCPS_JWT_PUBLIC_KEY",
        "BOND_MCPS_JWT_ISSUER",
        "BOND_MCPS_JWT_AUDIENCE",
        "BOND_MCPS_JWT_ALGORITHM",
        "BOND_MCPS_PUBLIC_URL",
        "BOND_MCPS_JWT_SUB_CLAIM",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOND_MCPS_JWT_PUBLIC_KEY", SECRET)
    monkeypatch.setenv("BOND_MCPS_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("BOND_MCPS_JWT_ISSUER", "bond-ai")
    monkeypatch.setenv("BOND_MCPS_JWT_AUDIENCE", "mcp-server")


def _bond_token(*, secret=SECRET, iss="bond-ai", aud=None, exp_delta=3600, sub="alice@example.com"):
    payload = {
        "sub": sub,
        "iss": iss,
        "aud": ["bond-ai-api", "mcp-server"] if aud is None else aud,
        "exp": int(time.time()) + exp_delta,
        "jti": "test-jti",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _verify(token):
    """Return the AccessToken on success, or None if the verifier rejects it."""
    verifier = build_verifier()
    try:
        return asyncio.run(verifier.verify_token(token))
    except Exception:  # noqa: BLE001 - some verifier versions raise instead of returning None
        return None


class TestHs256Verification:
    def test_accept_valid_bond_jwt(self):
        result = _verify(_bond_token())
        assert result is not None
        assert result.claims.get("sub") == "alice@example.com"

    def test_reject_wrong_secret(self):
        assert _verify(_bond_token(secret="not-the-secret")) is None

    def test_reject_wrong_issuer(self):
        assert _verify(_bond_token(iss="evil-issuer")) is None

    def test_reject_wrong_audience(self):
        assert _verify(_bond_token(aud=["some-other-aud"])) is None

    def test_reject_expired(self):
        assert _verify(_bond_token(exp_delta=-60)) is None

    def test_accept_when_aud_contains_configured_even_with_canonical(self, monkeypatch):
        # With BOND_MCPS_PUBLIC_URL set, _resolve_audience also accepts
        # "<public_url>/mcp". A token whose aud is ["bond-ai-api","mcp-server"]
        # must still pass because it contains the configured "mcp-server".
        monkeypatch.setenv("BOND_MCPS_PUBLIC_URL", "http://localhost:18003")
        assert _verify(_bond_token()) is not None
