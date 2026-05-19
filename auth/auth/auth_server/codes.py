"""One-shot OAuth authorization codes + pending upstream auth state.

Two storage classes wrap the ``oauth_auth_codes`` and ``oauth_pending_auth``
tables. The opaque token values are never stored in plaintext:

* Auth codes and refresh tokens go in as SHA-256 base64url fingerprints.
  An attacker with read access to the DB can't replay them at the AS.
* The upstream PKCE verifier (used between the AS and Cognito/Okta) is
  AEAD-encrypted via the existing ``auth.encryption`` module so a stolen
  snapshot can't be used to complete the upstream leg.

Both stores enforce single-use semantics via an atomic UPDATE ... WHERE
used_at IS NULL idiom (works on SQLite and Postgres). Expired rows are
swept lazily on each insert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update

from auth import encryption
from auth.db.models import OAuthAuthCode, OAuthPendingAuth, OAuthRefreshToken
from auth.db.repository import build_default_resolver
from auth.db.session import get_session
from auth.oauth_utils import generate_opaque_secret, sha256_b64u

AUTH_CODE_TTL_SECONDS = 60
PENDING_AUTH_TTL_SECONDS = 600
# Refresh-token TTL is a sliding window: each successful refresh issues a
# fresh token with a new 30-day clock. Users who use the system at all
# during 30 days never get prompted. Long enough to be invisible; short
# enough that a stale workstation isn't a long-lived attack window.
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600


class AuthCodeError(RuntimeError):
    """Code lookup / consumption failed (unknown, expired, or already used)."""


# ---------------------------------------------------------------------------
# Pending upstream auth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingAuth:
    bond_state: str
    client_id: str
    redirect_uri: str
    client_state: str | None
    code_challenge: str
    code_challenge_method: str
    resource: str | None
    scope: str | None
    upstream_code_verifier: str


def store_pending_auth(
    *,
    client_id: str,
    redirect_uri: str,
    client_state: str | None,
    code_challenge: str,
    code_challenge_method: str,
    resource: str | None,
    scope: str | None,
    upstream_code_verifier: str,
) -> str:
    """Persist an in-flight ``/oauth/authorize`` request, returning bond_state.

    The caller uses ``bond_state`` as the upstream IdP's ``state`` parameter.
    """
    bond_state = generate_opaque_secret(32)
    now = datetime.now(timezone.utc)
    blob, key_version = encryption.encrypt(
        upstream_code_verifier.encode("utf-8"),
        user_key="__pending__",
        provider="oauth_as",
        field="upstream_code_verifier",
        resolver=build_default_resolver(),
    )
    with get_session() as session:
        _sweep_pending_auth(session, now)
        session.add(
            OAuthPendingAuth(
                bond_state=bond_state,
                client_id=client_id,
                redirect_uri=redirect_uri,
                client_state=client_state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                resource=resource,
                scope=scope,
                upstream_code_verifier_encrypted=blob,
                key_version=key_version,
                expires_at=now + timedelta(seconds=PENDING_AUTH_TTL_SECONDS),
            )
        )
    return bond_state


def consume_pending_auth(bond_state: str) -> PendingAuth:
    """Atomically claim+return the pending row.

    Implemented as ``DELETE ... RETURNING`` so two concurrent callbacks with
    the same ``bond_state`` cannot both pass — the loser sees zero rows
    returned and raises. Supported on SQLite >= 3.35 and Postgres.
    """
    now = datetime.now(timezone.utc)
    with get_session() as session:
        stmt = (
            delete(OAuthPendingAuth)
            .where(OAuthPendingAuth.bond_state == bond_state)
            .returning(
                OAuthPendingAuth.bond_state,
                OAuthPendingAuth.client_id,
                OAuthPendingAuth.redirect_uri,
                OAuthPendingAuth.client_state,
                OAuthPendingAuth.code_challenge,
                OAuthPendingAuth.code_challenge_method,
                OAuthPendingAuth.resource,
                OAuthPendingAuth.scope,
                OAuthPendingAuth.upstream_code_verifier_encrypted,
                OAuthPendingAuth.key_version,
                OAuthPendingAuth.expires_at,
            )
        )
        row = session.execute(stmt).first()
        if row is None:
            raise AuthCodeError("Unknown or already-consumed authorize state.")
        if _aware(row.expires_at) < now:
            raise AuthCodeError("Authorize state expired; restart sign-in.")
        verifier = encryption.decrypt(
            row.upstream_code_verifier_encrypted,
            user_key="__pending__",
            provider="oauth_as",
            field="upstream_code_verifier",
            key_version=row.key_version,
            resolver=build_default_resolver(),
        ).decode("utf-8")
        return PendingAuth(
            bond_state=row.bond_state,
            client_id=row.client_id,
            redirect_uri=row.redirect_uri,
            client_state=row.client_state,
            code_challenge=row.code_challenge,
            code_challenge_method=row.code_challenge_method,
            resource=row.resource,
            scope=row.scope,
            upstream_code_verifier=verifier,
        )


# ---------------------------------------------------------------------------
# Issued auth codes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedAuthCode:
    """Returned by ``consume_auth_code`` after PKCE verification."""

    client_id: str
    user_key: str
    email: str | None
    redirect_uri: str
    resource: str | None
    scope: str | None


def issue_auth_code(
    *,
    client_id: str,
    user_key: str,
    email: str | None,
    code_challenge: str,
    code_challenge_method: str,
    redirect_uri: str,
    resource: str | None,
    scope: str | None,
) -> str:
    """Persist a fresh code, return the opaque value to send to the client."""
    code = generate_opaque_secret(32)
    code_hash = sha256_b64u(code)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        _sweep_auth_codes(session, now)
        session.add(
            OAuthAuthCode(
                code_hash=code_hash,
                client_id=client_id,
                user_key=user_key,
                email=email,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                redirect_uri=redirect_uri,
                resource=resource,
                scope=scope,
                expires_at=now + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
            )
        )
    return code


def consume_auth_code(
    code: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> IssuedAuthCode:
    """Atomically mark a code used and return its bindings.

    Verifies PKCE (S256), client_id, and redirect_uri against the stored
    values. Raises ``AuthCodeError`` on any mismatch / expiry / replay.
    """
    from auth.oauth_utils import verify_pkce_s256

    code_hash = sha256_b64u(code)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        # Atomic single-use enforcement: UPDATE ... WHERE used_at IS NULL.
        updated = session.execute(
            update(OAuthAuthCode)
            .where(OAuthAuthCode.code_hash == code_hash, OAuthAuthCode.used_at.is_(None))
            .values(used_at=now)
        ).rowcount
        if not updated:
            existing = session.get(OAuthAuthCode, code_hash)
            if existing is None:
                raise AuthCodeError("Unknown authorization code.")
            raise AuthCodeError("Authorization code already used.")

        row = session.get(OAuthAuthCode, code_hash)
        if _aware(row.expires_at) < now:
            raise AuthCodeError("Authorization code expired.")
        if row.client_id != client_id:
            raise AuthCodeError("client_id does not match authorization code.")
        if row.redirect_uri != redirect_uri:
            raise AuthCodeError("redirect_uri does not match authorization code.")
        if row.code_challenge_method != "S256":
            raise AuthCodeError("Unsupported code_challenge_method.")
        if not verify_pkce_s256(
            code_verifier=code_verifier, code_challenge=row.code_challenge
        ):
            raise AuthCodeError("PKCE verification failed.")

        return IssuedAuthCode(
            client_id=row.client_id,
            user_key=row.user_key,
            email=row.email,
            redirect_uri=row.redirect_uri,
            resource=row.resource,
            scope=row.scope,
        )


# ---------------------------------------------------------------------------
# Refresh tokens (RFC 6749 §6 + §10.4 rotation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedRefreshToken:
    """Returned by ``consume_refresh_token`` after validation."""

    client_id: str
    user_key: str
    resource: str | None
    scope: str | None


def issue_refresh_token(
    *,
    client_id: str,
    user_key: str,
    resource: str | None,
    scope: str | None,
) -> str:
    """Persist a fresh refresh-token hash, return the opaque value."""
    token = generate_opaque_secret(48)
    token_hash = sha256_b64u(token)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        _sweep_refresh_tokens(session, now)
        session.add(
            OAuthRefreshToken(
                token_hash=token_hash,
                client_id=client_id,
                user_key=user_key,
                resource=resource,
                scope=scope,
                expires_at=now + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
            )
        )
    return token


def consume_refresh_token(
    refresh_token: str,
    *,
    client_id: str,
) -> IssuedRefreshToken:
    """Atomically revoke the supplied refresh token and return its bindings.

    The caller mints a *new* refresh token after this returns — refresh
    token rotation per RFC 6749 §10.4 and OAuth 2.1's strong recommendation
    for public clients.
    """
    token_hash = sha256_b64u(refresh_token)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        # Atomic revoke: UPDATE ... WHERE revoked_at IS NULL.
        updated = session.execute(
            update(OAuthRefreshToken)
            .where(
                OAuthRefreshToken.token_hash == token_hash,
                OAuthRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        ).rowcount
        if not updated:
            existing = session.get(OAuthRefreshToken, token_hash)
            if existing is None:
                raise AuthCodeError("Unknown refresh token.")
            raise AuthCodeError("Refresh token has been revoked.")

        row = session.get(OAuthRefreshToken, token_hash)
        if _aware(row.expires_at) < now:
            raise AuthCodeError("Refresh token expired; sign in again.")
        if row.client_id != client_id:
            raise AuthCodeError("Refresh token was issued to a different client.")
        return IssuedRefreshToken(
            client_id=row.client_id,
            user_key=row.user_key,
            resource=row.resource,
            scope=row.scope,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _sweep_refresh_tokens(session, now: datetime) -> None:
    """Remove long-expired rows. Revoked-but-recent rows are kept around
    so a replayed token returns a useful 'already revoked' error rather
    than a generic 'unknown token'."""
    cutoff = now - timedelta(days=7)
    session.query(OAuthRefreshToken).filter(
        OAuthRefreshToken.expires_at < cutoff
    ).delete(synchronize_session=False)


def _aware(value: datetime) -> datetime:
    """Coerce a (possibly TZ-naive) DB datetime to UTC-aware.

    SQLite drops tzinfo on the way through; Postgres preserves it. Centralising
    the coercion avoids spurious comparison errors across dialects.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _sweep_pending_auth(session, now: datetime) -> None:
    session.query(OAuthPendingAuth).filter(
        OAuthPendingAuth.expires_at < now
    ).delete(synchronize_session=False)


def _sweep_auth_codes(session, now: datetime) -> None:
    cutoff = now - timedelta(minutes=10)
    session.query(OAuthAuthCode).filter(
        OAuthAuthCode.expires_at < cutoff
    ).delete(synchronize_session=False)
