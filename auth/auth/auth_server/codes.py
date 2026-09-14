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

Refresh tokens rotate: ``rotate_refresh_token`` revokes the presented token,
mints its successor and records the successor's hash on the revoked row, all
in one transaction. That record is what lets a rotation whose response never
reached the client be honoured a second time while the successor sits unused
-- see ``refresh_grace_seconds``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update

from auth import encryption
from auth.db.models import OAuthAuthCode, OAuthPendingAuth, OAuthRefreshToken
from auth.db.repository import build_default_resolver
from auth.db.session import get_session
from auth.oauth_utils import generate_opaque_secret, sha256_b64u

logger = logging.getLogger(__name__)

AUTH_CODE_TTL_SECONDS = 60
PENDING_AUTH_TTL_SECONDS = 600
# Refresh-token TTL is a sliding window: each successful refresh issues a
# fresh token with a new 30-day clock. Users who use the system at all
# during 30 days never get prompted. Long enough to be invisible; short
# enough that a stale workstation isn't a long-lived attack window.
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600

_ENV_REFRESH_GRACE_SECONDS = "BOND_MCPS_AS_REFRESH_GRACE_SECONDS"
_DEFAULT_REFRESH_GRACE_SECONDS = 7 * 24 * 3600


class AuthCodeError(RuntimeError):
    """Code lookup / consumption failed (unknown, expired, or already used).

    ``reason`` is an optional stable tag the token endpoint surfaces as
    ``error_reason`` beside ``error_description``. Clients that want to tell
    an expired session from a revoked one need something that does not rot
    when the prose is reworded.
    """

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


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
        if not verify_pkce_s256(code_verifier=code_verifier, code_challenge=row.code_challenge):
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


def refresh_grace_seconds() -> int:
    """How long after revocation a rotated refresh token may still be used.

    A refresh POST that reaches the AS but whose response is lost (lid
    closed, VPN dropping in, the app quit mid-launch) leaves the client
    holding a token the AS has already revoked. Its only move is to present
    that token again, which is indistinguishable from a replay unless the AS
    remembers what replaced it. It does: while the recorded successor has
    never been used, the presentation is honoured (see
    ``rotate_refresh_token``). This window bounds that offer in wall-clock
    time; the unused-successor rule is the safety property, since a stolen
    token cannot be graced once the real client has moved on.

    Default 7 days. ``bond-mcps prune-oauth`` keeps revoked rows one day past
    this window unless told otherwise, so the grace never promises something
    the database has already forgotten. ``0`` restores strict single-use
    rotation. Operators wanting Okta's posture set 30.
    Anything above the refresh token's own lifetime is clamped to it: past
    that the token has expired anyway, and an absurd value would overflow
    ``timedelta``.
    """
    raw = (os.environ.get(_ENV_REFRESH_GRACE_SECONDS) or "").strip()
    if not raw:
        return _DEFAULT_REFRESH_GRACE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default %ss",
            _ENV_REFRESH_GRACE_SECONDS,
            raw,
            _DEFAULT_REFRESH_GRACE_SECONDS,
        )
        return _DEFAULT_REFRESH_GRACE_SECONDS
    if value < 0:
        logger.warning(
            "%s=%r is negative; using default %ss",
            _ENV_REFRESH_GRACE_SECONDS,
            raw,
            _DEFAULT_REFRESH_GRACE_SECONDS,
        )
        return _DEFAULT_REFRESH_GRACE_SECONDS
    if value > REFRESH_TOKEN_TTL_SECONDS:
        logger.warning(
            "%s=%r exceeds the refresh token lifetime; clamping to %ss",
            _ENV_REFRESH_GRACE_SECONDS,
            raw,
            REFRESH_TOKEN_TTL_SECONDS,
        )
        return REFRESH_TOKEN_TTL_SECONDS
    return value


@dataclass(frozen=True)
class RotatedRefreshToken:
    """Returned by ``rotate_refresh_token``: bindings plus the successor.

    ``refresh_token`` is the opaque successor the caller must hand back to
    the client. A graced rotation (the presented token was already revoked
    and honoured because its successor was never used) returns the same
    shape as a plain one, deliberately: the client cannot tell and need not.
    """

    client_id: str
    user_key: str
    resource: str | None
    scope: str | None
    refresh_token: str


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


def rotate_refresh_token(
    refresh_token: str,
    *,
    client_id: str,
) -> RotatedRefreshToken:
    """Revoke the presented refresh token and mint its successor, atomically.

    Rotation per RFC 6749 §10.4 and OAuth 2.1's strong recommendation for
    public clients. Revoke, mint and the successor pointer all happen in one
    transaction, so there is no instant where the presented token is revoked
    but names no successor — a retry landing in that gap would be refused and
    would cost the user a sign-in.

    A token that is already revoked is honoured once more when its recorded
    successor has never been used and the grace window has not closed: that
    is a lost response, not a replay. Honouring it retires the unused
    successor, so the offer is good exactly once per lost response.

    Every refusal raises ``AuthCodeError`` with a ``reason`` and rolls the
    whole transaction back — a refused presentation must change nothing.

    Two simultaneous presentations of the same *live* token both succeed. The
    loser blocks on the row lock, finds the revoke matched nothing, takes the
    grace path and retires the winner's successor — so whoever received the
    first reply is holding a dead token and is refused (``revoked``) on its
    next refresh. That is the one case where the grace costs a sign-in strict
    rotation would not have. A well-behaved client never has two refreshes in
    flight (Bond Desktop coalesces them), so it surfaces a client bug late
    rather than a defect here.
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
        # `row` can only be None when the revoke matched nothing: a matched
        # row exists by definition, which is why the `updated` branch below
        # dereferences it unguarded.
        row = session.get(OAuthRefreshToken, token_hash)

        if updated:
            if _aware(row.expires_at) < now:
                raise AuthCodeError("Refresh token expired; sign in again.", reason="expired")
            if row.client_id != client_id:
                raise AuthCodeError(
                    "Refresh token was issued to a different client.",
                    reason="client_mismatch",
                )
        else:
            if row is None:
                raise AuthCodeError("Unknown refresh token.", reason="unknown")
            revoked = "Refresh token has been revoked."
            grace = refresh_grace_seconds()
            if grace <= 0:
                raise AuthCodeError(revoked, reason="revoked")
            if row.replaced_by_hash is None:
                raise AuthCodeError(revoked, reason="revoked")
            if row.revoked_at is None or now - _aware(row.revoked_at) > timedelta(seconds=grace):
                raise AuthCodeError(revoked, reason="revoked")
            # The bindings are still the presented row's, so they are still
            # checked before anything is retired.
            if _aware(row.expires_at) < now:
                raise AuthCodeError("Refresh token expired; sign in again.", reason="expired")
            if row.client_id != client_id:
                raise AuthCodeError(
                    "Refresh token was issued to a different client.",
                    reason="client_mismatch",
                )
            # Retiring the successor is the atomic test for "never used": if
            # the client did receive it and rotated with it, or a concurrent
            # grace got there first, this updates zero rows and the
            # presentation is a genuine replay.
            retired = session.execute(
                update(OAuthRefreshToken)
                .where(
                    OAuthRefreshToken.token_hash == row.replaced_by_hash,
                    OAuthRefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            ).rowcount
            if retired != 1:
                raise AuthCodeError(revoked, reason="revoked")
            # The age is the operator's signal: a lost response is retried
            # within minutes or at the next launch, so a grace days after the
            # revocation is worth a second look.
            logger.info(
                "refresh token rotation graced for client %s (successor unused, revoked %ds ago)",
                row.client_id,
                int((now - _aware(row.revoked_at)).total_seconds()),
            )

        # Mint the successor in this same transaction. On the grace path the
        # presented row's revoked_at is left alone: the window counts from the
        # first revocation, so a client stuck offline cannot renew it forever.
        new_token = generate_opaque_secret(48)
        new_hash = sha256_b64u(new_token)
        _sweep_refresh_tokens(session, now)
        session.add(
            OAuthRefreshToken(
                token_hash=new_hash,
                client_id=row.client_id,
                user_key=row.user_key,
                resource=row.resource,
                scope=row.scope,
                expires_at=now + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
            )
        )
        row.replaced_by_hash = new_hash
        return RotatedRefreshToken(
            client_id=row.client_id,
            user_key=row.user_key,
            resource=row.resource,
            scope=row.scope,
            refresh_token=new_token,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _sweep_refresh_tokens(session, now: datetime) -> None:
    """Remove long-expired rows. Revoked-but-recent rows are kept around so
    a replayed token returns a useful 'already revoked' error rather than a
    generic 'unknown token' — and so the lost-response grace in
    ``rotate_refresh_token`` can still find the successor they name."""
    cutoff = now - timedelta(days=7)
    session.query(OAuthRefreshToken).filter(OAuthRefreshToken.expires_at < cutoff).delete(
        synchronize_session=False
    )


def _aware(value: datetime) -> datetime:
    """Coerce a (possibly TZ-naive) DB datetime to UTC-aware.

    SQLite drops tzinfo on the way through; Postgres preserves it. Centralising
    the coercion avoids spurious comparison errors across dialects.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _sweep_pending_auth(session, now: datetime) -> None:
    session.query(OAuthPendingAuth).filter(OAuthPendingAuth.expires_at < now).delete(
        synchronize_session=False
    )


def _sweep_auth_codes(session, now: datetime) -> None:
    cutoff = now - timedelta(minutes=10)
    session.query(OAuthAuthCode).filter(OAuthAuthCode.expires_at < cutoff).delete(
        synchronize_session=False
    )
