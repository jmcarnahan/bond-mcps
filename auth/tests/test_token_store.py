"""Tests for the DB-backed TokenStore facade."""

import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from auth.token_store import TokenStore


@pytest.fixture
def store(repo):
    # `repo` fixture from conftest.py ensures DB + encryption key are set up.
    return TokenStore("test", user_key="alice")


class TestSaveAndLoad:
    def test_round_trip(self, store):
        store.save_token(
            {
                "access_token": "tok123",
                "refresh_token": "ref456",
                "expires_at": time.time() + 3600,
            }
        )
        loaded = store.get_token()
        assert loaded["access_token"] == "tok123"
        assert loaded["refresh_token"] == "ref456"

    def test_returns_none_if_missing(self, store):
        assert store.get_token() is None

    def test_returns_none_if_expired(self, store):
        store.save_token({"access_token": "old", "expires_at": time.time() - 100})
        assert store.get_token() is None

    def test_returns_token_without_expiry(self, store):
        store.save_token({"access_token": "forever"})
        loaded = store.get_token()
        assert loaded["access_token"] == "forever"


class TestClear:
    def test_clear_removes_row(self, store):
        store.save_token({"access_token": "x"})
        store.clear()
        assert store.get_token() is None

    def test_clear_no_error_if_missing(self, store):
        store.clear()  # should not raise


class TestRefresh:
    def test_refresh_saves_new_token(self, store):
        store.save_token(
            {
                "access_token": "old",
                "refresh_token": "ref123",
                "expires_at": time.time() - 100,
            }
        )

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {
                    "access_token": "new_tok",
                    "refresh_token": "ref456",
                    "expires_in": 3600,
                }
            ).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
            token = store.refresh_if_needed("cid", "secret", "https://token.url")

        assert token == "new_tok"
        saved = store.get_token()
        assert saved["access_token"] == "new_tok"
        assert saved["refresh_token"] == "ref456"

    def test_refresh_returns_none_without_refresh_token(self, store):
        store.save_token({"access_token": "old", "expires_at": time.time() - 100})
        assert store.refresh_if_needed("cid", "secret", "https://token.url") is None

    def test_returns_valid_token_without_refresh(self, store):
        store.save_token({"access_token": "still_good", "expires_at": time.time() + 3600})
        assert store.refresh_if_needed("cid", "secret", "https://token.url") == "still_good"

    def test_refresh_returns_none_without_existing_token(self, store):
        """No row at all → nothing to refresh."""
        assert store.refresh_if_needed("cid", "secret", "https://token.url") is None

    def test_refresh_http_error_preserves_row(self, store):
        """A failed refresh (e.g. HTTP 400 invalid_grant) returns None but must
        leave the existing row — including its refresh_token — intact, so a
        revoked token is distinguishable from a never-connected one."""
        store.save_token(
            {
                "access_token": "old",
                "refresh_token": "ref123",
                "expires_at": time.time() - 100,
            }
        )

        def fail_urlopen(req, timeout=None):
            raise urllib.error.HTTPError("https://token.url", 400, "Bad Request", {}, None)

        with patch("auth.token_store.urllib.request.urlopen", side_effect=fail_urlopen):
            assert store.refresh_if_needed("cid", "secret", "https://token.url") is None

        # Row still present with the original refresh_token (get_token filters on
        # access-token expiry, so read the raw row via the repository).
        raw = store.repo.get_token(store.user_key, store.provider)
        assert raw is not None
        assert raw["refresh_token"] == "ref123"

    def test_refresh_uses_form_encoded_body(self, store):
        """Per RFC 6749 §4.1.3 the token endpoint request must be form-encoded."""
        store.save_token(
            {
                "access_token": "old",
                "refresh_token": "ref123",
                "expires_at": time.time() - 100,
            }
        )

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            captured["content_type"] = req.headers.get("Content-type")
            captured["accept"] = req.headers.get("Accept")
            captured["user_agent"] = req.headers.get("User-agent")
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {
                    "access_token": "new_tok",
                    "expires_in": 3600,
                }
            ).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
            store.refresh_if_needed("cid", "secret", "https://token.url")

        assert captured["content_type"] == "application/x-www-form-urlencoded"
        assert captured["accept"] == "application/json"
        # Cloudflare-fronted token endpoints (e.g. WorkOS AuthKit) 403-ban
        # default Python UAs; a browser-like UA must be sent.
        from auth.token_store import OUTBOUND_USER_AGENT

        assert captured["user_agent"] == OUTBOUND_USER_AGENT
        body_str = captured["body"].decode()
        assert "grant_type=refresh_token" in body_str
        assert "client_id=cid" in body_str
        assert "refresh_token=ref123" in body_str

    def test_refresh_preserves_extra_metadata(self, store):
        """cloud_id and similar extras survive a refresh round-trip."""
        store.save_token(
            {
                "access_token": "old",
                "refresh_token": "ref123",
                "expires_at": time.time() - 100,
                "cloud_id": "atlassian-cloud-uuid",
            }
        )

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {
                    "access_token": "new_tok",
                    "expires_in": 3600,
                }
            ).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
            store.refresh_if_needed("cid", "secret", "https://token.url")

        saved = store.get_token()
        assert saved["cloud_id"] == "atlassian-cloud-uuid"

    def test_refresh_preserves_refresh_token_when_response_omits_it(self, store):
        """RFC 6749 §6: an omitted refresh_token means the old one stays valid.

        Figma's refresh response carries no refresh_token or scope. Nulling
        them would make the second refresh impossible, so refreshing twice is
        the real regression test.
        """
        store.save_token(
            {
                "access_token": "old",
                "refresh_token": "ref123",
                "expires_at": time.time() - 100,
                "scopes": "current_user:read file_comments:read",
            }
        )

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {"access_token": "new_tok", "expires_in": 3600}
            ).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
            assert store.refresh_if_needed("cid", "secret", "https://token.url") == "new_tok"

            saved = store.get_token()
            assert saved["refresh_token"] == "ref123"
            assert saved["scopes"] == "current_user:read file_comments:read"

            # Expire again and refresh a second time: this is what broke before.
            store.save_token({**saved, "expires_at": time.time() - 100})
            assert store.refresh_if_needed("cid", "secret", "https://token.url") == "new_tok"

        assert store.get_token()["refresh_token"] == "ref123"

    def test_refresh_rotated_refresh_token_replaces_old(self, store):
        """A provider that rotates its refresh_token still overwrites the old one."""
        store.save_token(
            {
                "access_token": "old",
                "refresh_token": "ref123",
                "expires_at": time.time() - 100,
            }
        )

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {"access_token": "new_tok", "refresh_token": "ref_v2", "expires_in": 3600}
            ).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
            store.refresh_if_needed("cid", "secret", "https://token.url")

        assert store.get_token()["refresh_token"] == "ref_v2"


class TestUserKeys:
    def test_two_users_keep_separate_state(self, repo):
        a = TokenStore("test", user_key="alice")
        b = TokenStore("test", user_key="bob")
        a.save_token({"access_token": "alice-token"})
        b.save_token({"access_token": "bob-token"})

        assert a.get_token()["access_token"] == "alice-token"
        assert b.get_token()["access_token"] == "bob-token"

    def test_user_key_defaults_to_env_var(self, repo, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_USER_ID", "charlie")
        s = TokenStore("test")
        s.save_token({"access_token": "charlie-token"})
        assert s.user_key == "charlie"

        # Reading back with explicit user_key="charlie" should match
        assert TokenStore("test", user_key="charlie").get_token()["access_token"] == "charlie-token"
