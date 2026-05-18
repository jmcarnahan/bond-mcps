"""Tests for the TokenRepository — CRUD, multi-user isolation, MSAL cache."""

import time

import pytest
from sqlalchemy import select

from auth.db.models import ProviderToken
from auth.db.repository import TokenRepository
from auth.db.session import get_session_factory
from auth.encryption import TokenEncryptionError


def test_save_then_get_round_trips(repo):
    repo.save_token(
        "alice",
        "github",
        {"access_token": "gho_abc"},
    )
    got = repo.get_token("alice", "github")
    assert got == {"access_token": "gho_abc"}


def test_save_with_refresh_and_expiry(repo):
    expires_at = time.time() + 3600
    repo.save_token(
        "alice",
        "atlassian",
        {
            "access_token": "atk-1",
            "refresh_token": "rtk-1",
            "expires_at": expires_at,
            "scopes": "read:jira-work write:jira-work",
            "cloud_id": "cloud-uuid-1",
        },
    )
    got = repo.get_token("alice", "atlassian")
    assert got["access_token"] == "atk-1"
    assert got["refresh_token"] == "rtk-1"
    assert abs(got["expires_at"] - expires_at) < 1.0
    assert got["scopes"] == "read:jira-work write:jira-work"
    assert got["cloud_id"] == "cloud-uuid-1"


def test_get_missing_returns_none(repo):
    assert repo.get_token("nobody", "github") is None


def test_save_is_upsert(repo):
    repo.save_token("alice", "github", {"access_token": "v1"})
    repo.save_token("alice", "github", {"access_token": "v2"})

    got = repo.get_token("alice", "github")
    assert got["access_token"] == "v2"

    # No duplicate rows
    factory = get_session_factory()
    with factory() as s:
        rows = s.execute(
            select(ProviderToken).where(
                ProviderToken.user_key == "alice",
                ProviderToken.provider == "github",
            )
        ).scalars().all()
    assert len(rows) == 1


def test_save_without_access_token_raises(repo):
    with pytest.raises(ValueError, match="access_token"):
        repo.save_token("alice", "github", {"refresh_token": "rtk"})


def test_multi_user_isolation(repo):
    repo.save_token("alice", "github", {"access_token": "alice-token"})
    repo.save_token("bob", "github", {"access_token": "bob-token"})

    assert repo.get_token("alice", "github")["access_token"] == "alice-token"
    assert repo.get_token("bob", "github")["access_token"] == "bob-token"


def test_multi_provider_per_user(repo):
    repo.save_token("alice", "github", {"access_token": "gh-tok"})
    repo.save_token("alice", "atlassian", {"access_token": "atl-tok"})

    assert repo.get_token("alice", "github")["access_token"] == "gh-tok"
    assert repo.get_token("alice", "atlassian")["access_token"] == "atl-tok"


def test_clear_removes_row(repo):
    repo.save_token("alice", "github", {"access_token": "tok"})
    repo.clear_token("alice", "github")
    assert repo.get_token("alice", "github") is None


def test_clear_missing_is_noop(repo):
    repo.clear_token("ghost", "github")  # should not raise


def test_extra_metadata_round_trip(repo):
    repo.save_token(
        "alice",
        "atlassian",
        {
            "access_token": "atk",
            "cloud_id": "cid",
            "site_url": "https://example.atlassian.net",
            "custom_field": {"nested": True},
        },
    )
    got = repo.get_token("alice", "atlassian")
    assert got["cloud_id"] == "cid"
    assert got["site_url"] == "https://example.atlassian.net"
    assert got["custom_field"] == {"nested": True}


def test_scopes_as_list_is_joined(repo):
    repo.save_token(
        "alice",
        "github",
        {"access_token": "tok", "scopes": ["repo", "user"]},
    )
    got = repo.get_token("alice", "github")
    assert got["scopes"] == "repo user"


def test_tampered_row_decryption_fails(repo, engine):
    """If a DBA edits the ciphertext, decrypt detects it via the auth tag."""
    repo.save_token("alice", "github", {"access_token": "tok"})

    factory = get_session_factory()
    with factory() as s:
        row = s.get(ProviderToken, ("alice", "github"))
        tampered = bytearray(row.access_token_encrypted)
        tampered[-1] ^= 0x01
        row.access_token_encrypted = bytes(tampered)
        s.commit()

    with pytest.raises(TokenEncryptionError):
        repo.get_token("alice", "github")


def test_ciphertext_is_actually_encrypted(repo, engine):
    """No plaintext token should ever appear in the DB column."""
    repo.save_token("alice", "github", {"access_token": "super-secret-plaintext"})

    factory = get_session_factory()
    with factory() as s:
        row = s.get(ProviderToken, ("alice", "github"))
        assert b"super-secret-plaintext" not in bytes(row.access_token_encrypted)


def test_msal_cache_round_trip(repo):
    blob = '{"AccessToken": {"home_account_id-login.microsoftonline.com-accesstoken-xxx": "data"}}'
    repo.save_msal_cache("alice", blob)
    assert repo.get_msal_cache("alice") == blob


def test_msal_cache_missing_returns_none(repo):
    assert repo.get_msal_cache("nobody") is None


def test_msal_cache_upsert(repo):
    repo.save_msal_cache("alice", '{"v": 1}')
    repo.save_msal_cache("alice", '{"v": 2}')
    assert repo.get_msal_cache("alice") == '{"v": 2}'


def test_msal_cache_clear(repo):
    repo.save_msal_cache("alice", '{"v": 1}')
    repo.clear_msal_cache("alice")
    assert repo.get_msal_cache("alice") is None


def test_msal_per_user_isolation(repo):
    repo.save_msal_cache("alice", '{"who": "alice"}')
    repo.save_msal_cache("bob", '{"who": "bob"}')
    assert repo.get_msal_cache("alice") == '{"who": "alice"}'
    assert repo.get_msal_cache("bob") == '{"who": "bob"}'


def test_locked_token_update(repo):
    repo.save_token(
        "alice",
        "atlassian",
        {"access_token": "old", "refresh_token": "rtk", "expires_at": time.time() - 10},
    )

    with repo.locked_token("alice", "atlassian") as locked:
        assert locked.data["access_token"] == "old"
        assert locked.is_expired()
        locked.update({
            "access_token": "new",
            "refresh_token": "rtk-2",
            "expires_at": time.time() + 3600,
        })
        assert locked.data["access_token"] == "new"

    got = repo.get_token("alice", "atlassian")
    assert got["access_token"] == "new"
    assert got["refresh_token"] == "rtk-2"


def test_locked_token_preserves_extra_metadata(repo):
    """Refresh shouldn't lose cloud_id and similar extras from the old row."""
    repo.save_token(
        "alice",
        "atlassian",
        {
            "access_token": "old",
            "refresh_token": "rtk",
            "expires_at": time.time() - 10,
            "cloud_id": "cid-1",
        },
    )
    with repo.locked_token("alice", "atlassian") as locked:
        locked.update({"access_token": "new", "refresh_token": "rtk-2",
                       "expires_at": time.time() + 3600})

    got = repo.get_token("alice", "atlassian")
    assert got["cloud_id"] == "cid-1"  # preserved across refresh


def test_locked_token_no_existing_row(repo):
    """locked_token on a missing row yields a context with data=None."""
    with repo.locked_token("ghost", "github") as locked:
        assert locked.data is None
        assert not locked.is_expired()
