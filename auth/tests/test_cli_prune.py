"""Tests for `bond-mcps prune-oauth` and `bond-mcps revoke-tokens`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auth.alembic_config import upgrade_head
from auth.cli import main
from auth.db import reset_for_tests
from auth.db.models import (
    OAuthAuthCode,
    OAuthClient,
    OAuthPendingAuth,
    OAuthRefreshToken,
)
from auth.db.session import get_session


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    reset_for_tests()
    upgrade_head()
    yield
    reset_for_tests()


def _row_count(model) -> int:
    with get_session() as s:
        return s.query(model).count()


class TestPruneOAuth:
    def test_deletes_expired_pending_auth(self, fresh_db):
        now = datetime.now(timezone.utc)
        with get_session() as s:
            s.add(
                OAuthPendingAuth(
                    bond_state="expired-pending",
                    client_id="cid",
                    redirect_uri="x",
                    code_challenge="c",
                    upstream_code_verifier_encrypted=b"x",
                    key_version=1,
                    expires_at=now - timedelta(hours=1),
                )
            )
            s.add(
                OAuthPendingAuth(
                    bond_state="fresh-pending",
                    client_id="cid",
                    redirect_uri="x",
                    code_challenge="c",
                    upstream_code_verifier_encrypted=b"x",
                    key_version=1,
                    expires_at=now + timedelta(hours=1),
                )
            )

        assert main(["prune-oauth"]) == 0
        assert _row_count(OAuthPendingAuth) == 1  # only fresh-pending survives

    def test_deletes_expired_auth_codes(self, fresh_db):
        now = datetime.now(timezone.utc)
        with get_session() as s:
            s.add(
                OAuthAuthCode(
                    code_hash="expired",
                    client_id="cid",
                    user_key="u",
                    code_challenge="c",
                    redirect_uri="x",
                    expires_at=now - timedelta(hours=1),
                )
            )
            s.add(
                OAuthAuthCode(
                    code_hash="fresh",
                    client_id="cid",
                    user_key="u",
                    code_challenge="c",
                    redirect_uri="x",
                    expires_at=now + timedelta(minutes=5),
                )
            )

        assert main(["prune-oauth"]) == 0
        assert _row_count(OAuthAuthCode) == 1

    def test_deletes_long_revoked_refresh_tokens(self, fresh_db):
        now = datetime.now(timezone.utc)
        with get_session() as s:
            # Revoked 14 days ago — past the 7-day grace
            s.add(
                OAuthRefreshToken(
                    token_hash="old-revoked",
                    client_id="cid",
                    user_key="u",
                    revoked_at=now - timedelta(days=14),
                    expires_at=now + timedelta(days=16),
                )
            )
            # Revoked 1 day ago — within grace
            s.add(
                OAuthRefreshToken(
                    token_hash="recent-revoked",
                    client_id="cid",
                    user_key="u",
                    revoked_at=now - timedelta(days=1),
                    expires_at=now + timedelta(days=29),
                )
            )
            # Active
            s.add(
                OAuthRefreshToken(
                    token_hash="active",
                    client_id="cid",
                    user_key="u",
                    expires_at=now + timedelta(days=30),
                )
            )

        assert main(["prune-oauth"]) == 0
        assert _row_count(OAuthRefreshToken) == 2  # old-revoked deleted

    def test_deletes_idle_dcr_clients_but_keeps_active(self, fresh_db):
        now = datetime.now(timezone.utc)
        with get_session() as s:
            # Idle client — created 60 days ago, no recent refresh tokens
            s.add(
                OAuthClient(
                    client_id="idle",
                    redirect_uris=["http://localhost/cb"],
                    grant_types=["authorization_code"],
                    response_types=["code"],
                    is_static=False,
                    created_at=now - timedelta(days=60),
                )
            )
            # Active client — also old, but has a recent refresh token
            s.add(
                OAuthClient(
                    client_id="active",
                    redirect_uris=["http://localhost/cb"],
                    grant_types=["authorization_code"],
                    response_types=["code"],
                    is_static=False,
                    created_at=now - timedelta(days=60),
                )
            )
            s.add(
                OAuthRefreshToken(
                    token_hash="rt-for-active",
                    client_id="active",
                    user_key="u",
                    created_at=now - timedelta(days=2),
                    expires_at=now + timedelta(days=28),
                )
            )
            # Static client — never pruned regardless of age
            s.add(
                OAuthClient(
                    client_id="static",
                    redirect_uris=["http://localhost/cb"],
                    grant_types=["authorization_code"],
                    response_types=["code"],
                    is_static=True,
                    created_at=now - timedelta(days=180),
                )
            )

        assert main(["prune-oauth"]) == 0
        survivors = {row.client_id for row in get_session().__enter__().query(OAuthClient).all()}
        assert "active" in survivors
        assert "static" in survivors
        assert "idle" not in survivors

    def test_dry_run_makes_no_changes(self, fresh_db):
        now = datetime.now(timezone.utc)
        with get_session() as s:
            s.add(
                OAuthPendingAuth(
                    bond_state="expired-pending",
                    client_id="cid",
                    redirect_uri="x",
                    code_challenge="c",
                    upstream_code_verifier_encrypted=b"x",
                    key_version=1,
                    expires_at=now - timedelta(hours=1),
                )
            )

        before = _row_count(OAuthPendingAuth)
        assert main(["prune-oauth", "--dry-run"]) == 0
        assert _row_count(OAuthPendingAuth) == before


class TestRevokeTokens:
    def test_revokes_only_for_specified_user(self, fresh_db):
        now = datetime.now(timezone.utc)
        with get_session() as s:
            s.add(
                OAuthRefreshToken(
                    token_hash="alice-1",
                    client_id="cid",
                    user_key="alice",
                    expires_at=now + timedelta(days=30),
                )
            )
            s.add(
                OAuthRefreshToken(
                    token_hash="alice-2",
                    client_id="cid",
                    user_key="alice",
                    expires_at=now + timedelta(days=30),
                )
            )
            s.add(
                OAuthRefreshToken(
                    token_hash="bob-1",
                    client_id="cid",
                    user_key="bob",
                    expires_at=now + timedelta(days=30),
                )
            )

        assert main(["revoke-tokens", "--user-key", "alice"]) == 0

        with get_session() as s:
            revoked = (
                s.query(OAuthRefreshToken).filter(OAuthRefreshToken.revoked_at.isnot(None)).count()
            )
            assert revoked == 2  # both alice rows
            bob_revoked = (
                s.query(OAuthRefreshToken)
                .filter(
                    OAuthRefreshToken.user_key == "bob",
                    OAuthRefreshToken.revoked_at.isnot(None),
                )
                .count()
            )
            assert bob_revoked == 0

    def test_idempotent_on_already_revoked(self, fresh_db):
        now = datetime.now(timezone.utc)
        with get_session() as s:
            s.add(
                OAuthRefreshToken(
                    token_hash="already-rev",
                    client_id="cid",
                    user_key="alice",
                    revoked_at=now - timedelta(hours=1),
                    expires_at=now + timedelta(days=30),
                )
            )

        # First call: nothing to revoke (already revoked)
        assert main(["revoke-tokens", "--user-key", "alice"]) == 0
        # Second call: same
        assert main(["revoke-tokens", "--user-key", "alice"]) == 0
