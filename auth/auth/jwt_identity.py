"""JWT-based caller identity for multi-tenant deployments.

Activated by setting BOND_MCPS_JWT_PUBLIC_KEY. When active, the per-request
user_key is derived from the `sub` (or configured) claim of an X-Bond-Auth:
Bearer <jwt> header rather than from the BOND_MCPS_USER_ID env var. The
signature is verified against the operator-supplied public key, and the
iss/aud claims are checked when configured.

This is independent of the provider Bearer token that bond-ai forwards in
the standard `Authorization` header — that one is the GitHub / MS Graph /
etc. access token, used directly against the provider API. X-Bond-Auth
identifies WHO bond-ai is acting for, so each user's local-OAuth tokens
(Path 2 fallback) get keyed correctly in the DB.

When BOND_MCPS_JWT_PUBLIC_KEY is unset, the deployment is treated as
single-tenant and `current_user_key()` (env-based) is used for everyone.
"""

from __future__ import annotations

import logging
import os

import jwt
from jwt import InvalidTokenError

logger = logging.getLogger(__name__)

ENV_PUBLIC_KEY = "BOND_MCPS_JWT_PUBLIC_KEY"
ENV_ISSUER     = "BOND_MCPS_JWT_ISSUER"
ENV_AUDIENCE   = "BOND_MCPS_JWT_AUDIENCE"
ENV_ALGORITHM  = "BOND_MCPS_JWT_ALGORITHM"
ENV_SUB_CLAIM  = "BOND_MCPS_JWT_SUB_CLAIM"

DEFAULT_ALGORITHM = "RS256"
DEFAULT_SUB_CLAIM = "sub"


class JWTConfigError(RuntimeError):
    """The JWT verification config is incomplete or malformed."""


class IdentityVerificationError(PermissionError):
    """A presented identity JWT failed verification (signature, claims, etc.)."""


def is_jwt_verification_enabled() -> bool:
    """True iff BOND_MCPS_JWT_PUBLIC_KEY is set (multi-tenant mode)."""
    return bool(os.environ.get(ENV_PUBLIC_KEY, "").strip())


def verify_identity_token(token: str) -> str:
    """Verify an identity JWT and return the resolved user_key.

    The configured claim (default ``sub``) becomes the user_key. The token's
    signature is verified against ``BOND_MCPS_JWT_PUBLIC_KEY``; iss and aud
    are checked when their respective env vars are set. ``exp`` is always
    required.

    Raises:
        JWTConfigError: BOND_MCPS_JWT_PUBLIC_KEY is unset (caller should
            check is_jwt_verification_enabled() first).
        IdentityVerificationError: signature invalid, claims missing or
            wrong, or token expired.
    """
    public_key = os.environ.get(ENV_PUBLIC_KEY, "").strip()
    if not public_key:
        raise JWTConfigError(f"{ENV_PUBLIC_KEY} is not set")

    issuer = os.environ.get(ENV_ISSUER, "").strip() or None
    audience = os.environ.get(ENV_AUDIENCE, "").strip() or None
    algorithm = os.environ.get(ENV_ALGORITHM, "").strip() or DEFAULT_ALGORITHM
    sub_claim = os.environ.get(ENV_SUB_CLAIM, "").strip() or DEFAULT_SUB_CLAIM

    decode_kwargs = {
        "algorithms": [algorithm],
        # exp is always required; sub_claim is required so we can build user_key.
        "options": {"require": [sub_claim, "exp"]},
    }
    if issuer is not None:
        decode_kwargs["issuer"] = issuer
    if audience is not None:
        decode_kwargs["audience"] = audience

    try:
        payload = jwt.decode(token, public_key, **decode_kwargs)
    except InvalidTokenError as e:
        # Don't echo the token itself in the error string.
        raise IdentityVerificationError(
            f"Identity JWT verification failed: {e}"
        ) from e

    user_key = payload.get(sub_claim)
    if user_key is None:
        # Should be unreachable given options.require, but defensive.
        raise IdentityVerificationError(
            f"Identity JWT payload is missing the {sub_claim!r} claim"
        )
    if not isinstance(user_key, str) or not user_key.strip():
        raise IdentityVerificationError(
            f"Identity JWT {sub_claim!r} claim must be a non-empty string"
        )
    return user_key
