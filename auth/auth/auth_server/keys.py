"""RSA signing key + JWKS document for the bond-mcps Authorization Server.

Key sources, in precedence order:

1. ``BOND_MCPS_AS_PRIVATE_KEY_PEM`` env var — full PEM string. **This is the
   production path**: External Secrets reads the JSON-shaped SM secret
   ``${env}-as-credentials`` and injects the ``BOND_MCPS_AS_PRIVATE_KEY_PEM``
   field as an env var on the AS pod. No volume mount, no on-disk PEM.
2. ``BOND_MCPS_AS_PRIVATE_KEY_FILE`` env var — path to a PEM file.
   Supported for advanced setups that want a volume-mounted file (e.g.
   tmpfs from a third-party secret store). Not used by the stock helm
   chart.
3. ``~/.bond_mcps/jwt_signing_key.pem`` (mode 0600). **Dev-only**: auto-
   generated on first use for the laptop flow. Refused when
   ``BOND_MCPS_DB_URL`` points at Postgres so a misconfigured deploy
   never silently runs on a freshly-generated key.

``BOND_MCPS_AS_PREVIOUS_KEY_PEM`` optionally provides a second key during
a rotation overlap window; both keys are published in the JWKS document
but only the current key is used for signing. The previous-key support is
included now because the cost is a few lines and rotation is the one
change you don't want to discover you can't make later.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

logger = logging.getLogger(__name__)

ENV_PRIVATE_KEY_PEM   = "BOND_MCPS_AS_PRIVATE_KEY_PEM"
ENV_PRIVATE_KEY_FILE  = "BOND_MCPS_AS_PRIVATE_KEY_FILE"
ENV_PREVIOUS_KEY_PEM  = "BOND_MCPS_AS_PREVIOUS_KEY_PEM"

_DEFAULT_KEY_PATH = Path.home() / ".bond_mcps" / "jwt_signing_key.pem"


class ASKeyError(RuntimeError):
    """The Authorization Server's signing key is missing or malformed."""


@dataclass(frozen=True)
class SigningKey:
    private_key: RSAPrivateKey
    kid: str

    @property
    def public_key(self) -> RSAPublicKey:
        return self.private_key.public_key()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_signing_key() -> SigningKey:
    """Resolve the current signing key, generating one on the fly if needed."""
    if pem := os.environ.get(ENV_PRIVATE_KEY_PEM, "").strip():
        return _from_pem(pem)

    if path_str := os.environ.get(ENV_PRIVATE_KEY_FILE, "").strip():
        return _from_file(Path(path_str))

    if _DEFAULT_KEY_PATH.exists():
        return _from_file(_DEFAULT_KEY_PATH)

    if _is_postgres_deployment():
        raise ASKeyError(
            f"Postgres deployment requires {ENV_PRIVATE_KEY_PEM} or "
            f"{ENV_PRIVATE_KEY_FILE} to be set explicitly. Auto-generation "
            "into the home directory is disabled outside SQLite dev."
        )

    return _autogenerate(_DEFAULT_KEY_PATH)


def load_previous_signing_key() -> SigningKey | None:
    """Optional previous key for JWKS rotation overlap."""
    pem = os.environ.get(ENV_PREVIOUS_KEY_PEM, "").strip()
    if not pem:
        return None
    return _from_pem(pem)


def build_jwks_document() -> dict:
    """Return the JWKS document published at ``/.well-known/jwks.json``."""
    keys = [_public_key_to_jwk(load_signing_key())]
    if prev := load_previous_signing_key():
        keys.append(_public_key_to_jwk(prev))
    return {"keys": keys}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _from_pem(pem: str) -> SigningKey:
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise ASKeyError(f"Invalid signing key PEM: {exc}") from exc
    if not isinstance(key, RSAPrivateKey):
        raise ASKeyError("Signing key must be RSA.")
    return SigningKey(private_key=key, kid=_compute_kid(key.public_key()))


def _from_file(path: Path) -> SigningKey:
    if not path.exists():
        raise ASKeyError(f"Signing key file not found: {path}")
    return _from_pem(path.read_text())


def _autogenerate(path: Path) -> SigningKey:
    logger.warning(
        "Generating an Authorization Server signing key at %s. "
        "This is appropriate for laptop dev only; set %s in deployments.",
        path,
        ENV_PRIVATE_KEY_PEM,
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not chmod 0600 the signing key at %s", path)
    return SigningKey(private_key=key, kid=_compute_kid(key.public_key()))


def _compute_kid(public_key: RSAPublicKey) -> str:
    """Stable kid: first 8 chars of SHA-256 over the SPKI DER."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:8]


def _public_key_to_jwk(signing_key: SigningKey) -> dict:
    """Serialize an RSA public key into a JWK dict per RFC 7517."""
    numbers = signing_key.public_key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": signing_key.kid,
        "n": _b64u_uint(numbers.n),
        "e": _b64u_uint(numbers.e),
    }


def _b64u_uint(value: int) -> str:
    """Big-endian unsigned int -> base64url-no-padding."""
    length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _is_postgres_deployment() -> bool:
    return os.environ.get("BOND_MCPS_DB_URL", "").startswith("postgres")


def all_active_keys() -> Iterable[SigningKey]:
    """Yield the current signing key plus any rotation-window previous key."""
    yield load_signing_key()
    if prev := load_previous_signing_key():
        yield prev
