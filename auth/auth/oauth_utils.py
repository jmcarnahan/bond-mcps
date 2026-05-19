"""OAuth 2.1 helpers shared by the bond-mcps Authorization Server.

Self-contained PKCE + state helpers, modelled on the ones in bond-ai's
``bondable.bond.auth.oauth_utils``. We don't import bond-ai because we
must run without it (bond-ai is just another client of bond-mcps).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 §4.

    The verifier is 64 url-safe bytes; the challenge is its SHA-256 digest
    base64url-encoded without padding (the S256 method).
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def verify_pkce_s256(*, code_verifier: str, code_challenge: str) -> bool:
    """Check ``BASE64URL(SHA256(verifier)) == challenge`` in constant time."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, code_challenge)


def generate_state() -> str:
    """High-entropy URL-safe state parameter."""
    return secrets.token_urlsafe(32)


def generate_opaque_secret(byte_length: int = 32) -> str:
    """High-entropy URL-safe random for opaque tokens, codes, tickets, etc."""
    return secrets.token_urlsafe(byte_length)


def sha256_b64u(value: str) -> str:
    """SHA-256 hash → base64url, used to store one-way fingerprints in DB."""
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
