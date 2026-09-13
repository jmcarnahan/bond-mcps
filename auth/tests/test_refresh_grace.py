"""A refresh whose response never arrived must not cost the user a sign-in.

Bond Desktop keeps its access JWT in memory only, so every launch rotates the
refresh token — and launch is exactly when the network is least reliable
(waking from sleep, VPN still coming up, the user quitting again). When that
response is lost the app still holds a token the AS has already revoked, and
strict single-use rotation answers the retry with ``invalid_grant``, which
clears the keychain slot and sends the user back through the browser.

``rotate_refresh_token`` closes that hole: a revoked token whose recorded
successor was never used is honoured once more. These tests pin both halves —
the lost response is forgiven, and a genuine replay (the successor was used)
is still refused.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from auth.auth_server.codes import (
    REFRESH_TOKEN_TTL_SECONDS,
    AuthCodeError,
    RotatedRefreshToken,
    issue_refresh_token,
    refresh_grace_seconds,
    rotate_refresh_token,
)
from auth.db.models import OAuthRefreshToken
from auth.db.session import get_session
from auth.oauth_utils import sha256_b64u

CLIENT = "bm-desktop-1"
USER = "alice@example.com"
RESOURCE = "http://github-mcp.test/mcp"


@pytest.fixture
def token(engine):
    """A live refresh token, as the code grant would have issued it."""
    return issue_refresh_token(client_id=CLIENT, user_key=USER, resource=RESOURCE, scope=None)


def _row(value: str) -> dict | None:
    """Read one token row into a plain dict, in a session of its own.

    A fresh session is the point: it proves what the rotation transaction
    actually committed, not what its own identity map remembers.
    """
    with get_session() as session:
        row = session.get(OAuthRefreshToken, sha256_b64u(value))
        if row is None:
            return None
        return {
            "client_id": row.client_id,
            "user_key": row.user_key,
            "revoked_at": row.revoked_at,
            "replaced_by_hash": row.replaced_by_hash,
        }


def _chain_rows() -> dict[str, dict]:
    """Every row this test user owns, keyed by token hash."""
    with get_session() as session:
        rows = session.query(OAuthRefreshToken).filter(OAuthRefreshToken.user_key == USER).all()
        return {
            row.token_hash: {
                "revoked_at": row.revoked_at,
                "replaced_by_hash": row.replaced_by_hash,
            }
            for row in rows
        }


def _shift(value: str, **columns) -> None:
    """Rewrite one token row's timestamps to stand in for elapsed time."""
    with get_session() as session:
        session.execute(
            update(OAuthRefreshToken)
            .where(OAuthRefreshToken.token_hash == sha256_b64u(value))
            .values(**columns)
        )


def test_a_lost_response_is_graced(token, monkeypatch):
    """The app rotated, the reply never arrived, it retries with what it has."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    first = rotate_refresh_token(token, client_id=CLIENT)
    assert first.graced is False

    retry = rotate_refresh_token(token, client_id=CLIENT)
    assert retry.graced is True
    assert retry.refresh_token != first.refresh_token
    assert retry.user_key == USER
    assert retry.resource == RESOURCE

    # The successor the client never received is retired, not left live.
    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(first.refresh_token, client_id=CLIENT)
    assert exc.value.reason == "revoked"

    # And the token the client did receive keeps working.
    assert rotate_refresh_token(retry.refresh_token, client_id=CLIENT).graced is False


def test_a_used_successor_is_not_graced(token, monkeypatch):
    """The real replay case: the client got the successor and moved on, so an
    older token turning up is a stolen one, not a lost response."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    first = rotate_refresh_token(token, client_id=CLIENT)
    second = rotate_refresh_token(first.refresh_token, client_id=CLIENT)

    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(token, client_id=CLIENT)
    assert exc.value.reason == "revoked"
    assert str(exc.value) == "Refresh token has been revoked."

    # The refusal changed nothing: the live token still rotates.
    assert rotate_refresh_token(second.refresh_token, client_id=CLIENT).graced is False


def test_grace_disabled_is_strict_single_use(token, monkeypatch):
    """`0` restores the old behaviour for operators who want it."""
    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", "0")
    rotate_refresh_token(token, client_id=CLIENT)

    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(token, client_id=CLIENT)
    assert exc.value.reason == "revoked"


def test_grace_window_elapsed(token, monkeypatch):
    """Past the window the offer closes, however unused the successor is."""
    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", "60")
    rotate_refresh_token(token, client_id=CLIENT)
    _shift(token, revoked_at=datetime.now(timezone.utc) - timedelta(seconds=120))

    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(token, client_id=CLIENT)
    assert exc.value.reason == "revoked"


def test_grace_window_counts_from_the_first_revocation(token, monkeypatch):
    """A client that is offline cannot renew a lost rotation forever.

    Each grace leaves the presented row's revoked_at alone, so the clock keeps
    running from the moment the token was first rotated rather than restarting
    on every retry.
    """
    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", "60")
    rotate_refresh_token(token, client_id=CLIENT)

    _shift(token, revoked_at=datetime.now(timezone.utc) - timedelta(seconds=45))
    assert rotate_refresh_token(token, client_id=CLIENT).graced is True

    _shift(token, revoked_at=datetime.now(timezone.utc) - timedelta(seconds=90))
    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(token, client_id=CLIENT)
    assert exc.value.reason == "revoked"


def test_client_mismatch_inside_grace_keeps_the_successor_live(token, monkeypatch):
    """A wrong client_id must not burn the successor a legitimate app holds.

    The bindings are checked before anything is retired, so the refusal never
    reaches the UPDATE that would kill the token a legitimate app is waiting
    to use.
    """
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    first = rotate_refresh_token(token, client_id=CLIENT)

    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(token, client_id="bm-someone-else")
    assert exc.value.reason == "client_mismatch"

    assert rotate_refresh_token(first.refresh_token, client_id=CLIENT).graced is False


def test_a_live_token_presented_by_the_wrong_client_is_not_revoked(token, monkeypatch):
    """The case that does lean on the rollback: the revoke has already run
    when the client check fails, and unwinding it is what keeps the user's
    live session alive after someone else fumbles a request."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(token, client_id="bm-someone-else")
    assert exc.value.reason == "client_mismatch"
    assert _row(token)["revoked_at"] is None

    assert rotate_refresh_token(token, client_id=CLIENT).graced is False


def test_expired_token_inside_grace(token, monkeypatch):
    """Thirty idle days is an honest sign-in; grace does not extend the TTL."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    rotate_refresh_token(token, client_id=CLIENT)
    _shift(token, expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(token, client_id=CLIENT)
    assert exc.value.reason == "expired"


def test_two_lost_responses_in_a_row(token, monkeypatch):
    """A flaky launch can lose two replies running; the app still holds T0."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    first = rotate_refresh_token(token, client_id=CLIENT)
    revoked_at = _row(token)["revoked_at"]

    second = rotate_refresh_token(token, client_id=CLIENT)
    third = rotate_refresh_token(token, client_id=CLIENT)
    assert second.graced is True
    assert third.graced is True
    assert len({first.refresh_token, second.refresh_token, third.refresh_token}) == 3
    # Each grace rewrites the successor pointer and nothing else. The window
    # is measured from the first revocation, so repeated graces cannot walk
    # it forward — see test_grace_window_counts_from_the_first_revocation.
    assert _row(token)["revoked_at"] == revoked_at

    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(second.refresh_token, client_id=CLIENT)
    assert exc.value.reason == "revoked"

    assert rotate_refresh_token(third.refresh_token, client_id=CLIENT).graced is False


def test_rotation_is_one_transaction(token, monkeypatch):
    """Revoked-but-names-no-successor is the state a retry must never find:
    a token stuck there would be refused and cost a sign-in."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    first = rotate_refresh_token(token, client_id=CLIENT)

    presented = _row(token)
    assert presented["revoked_at"] is not None
    assert presented["replaced_by_hash"] == sha256_b64u(first.refresh_token)

    successor = _row(first.refresh_token)
    assert successor is not None
    assert successor["revoked_at"] is None
    assert successor["replaced_by_hash"] is None
    assert successor["client_id"] == CLIENT
    assert successor["user_key"] == USER


def test_an_unknown_token_says_so(engine, monkeypatch):
    """`unknown` and `revoked` reach the desktop as different sentences."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token("never-issued-by-anyone", client_id=CLIENT)
    assert exc.value.reason == "unknown"
    assert str(exc.value) == "Unknown refresh token."


def test_concurrent_presentations_of_a_live_token(token, monkeypatch):
    """Two refreshes of the same live token at once — a client bug, not ours.

    One thread wins the atomic revoke; the other blocks on the row lock, finds
    the revoke matched nothing and takes the grace path, which retires the
    winner's successor. So both callers get a 200 and the one that was served
    FIRST is left holding a dead token. That is the single case where the
    grace costs a sign-in strict rotation would not have, and it is worth
    pinning: a well-behaved client never has two refreshes in flight.

    The assertions are the invariants that hold in every interleaving — which
    thread wins is up to SQLite, so nothing here depends on the order.
    """
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def worker(index):
        barrier.wait()
        try:
            results[index] = rotate_refresh_token(token, client_id=CLIENT)
        except Exception as exc:  # recorded, then asserted on the main thread
            results[index] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(isinstance(r, RotatedRefreshToken) for r in results), results
    winner = next(r for r in results if not r.graced)
    loser = next(r for r in results if r.graced)

    # Three rows: the presented token, the successor nobody will ever use,
    # and the live one the graced caller was handed.
    rows = _chain_rows()
    assert len(rows) == 3
    presented = rows[sha256_b64u(token)]
    assert presented["revoked_at"] is not None
    assert presented["replaced_by_hash"] == sha256_b64u(loser.refresh_token)

    stranded = rows[sha256_b64u(winner.refresh_token)]
    assert stranded["revoked_at"] is not None
    assert stranded["replaced_by_hash"] is None

    live = rows[sha256_b64u(loser.refresh_token)]
    assert live["revoked_at"] is None

    # The caller served first is signed out on its next refresh...
    with pytest.raises(AuthCodeError) as exc:
        rotate_refresh_token(winner.refresh_token, client_id=CLIENT)
    assert exc.value.reason == "revoked"
    # ...and the other one carries on.
    assert rotate_refresh_token(loser.refresh_token, client_id=CLIENT).graced is False


def test_grace_setting_parsing(monkeypatch):
    """A typo in the deployment env must not silently disable the grace."""
    monkeypatch.delenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", raising=False)
    assert refresh_grace_seconds() == 7 * 24 * 3600

    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", "30")
    assert refresh_grace_seconds() == 30

    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", "0")
    assert refresh_grace_seconds() == 0

    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", "a week")
    assert refresh_grace_seconds() == 7 * 24 * 3600

    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", "-60")
    assert refresh_grace_seconds() == 7 * 24 * 3600

    # Above the token's own 30-day lifetime the extra seconds are unreachable,
    # and an absurd value would overflow the timedelta the window is built on.
    monkeypatch.setenv("BOND_MCPS_AS_REFRESH_GRACE_SECONDS", str(31 * 24 * 3600))
    assert refresh_grace_seconds() == REFRESH_TOKEN_TTL_SECONDS
