"""Tests for the DB-backed TokenStore facade."""

import json
import time
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
