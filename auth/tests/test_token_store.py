"""Tests for token_store.py -- generic file-based token cache."""

import json
import time
from unittest.mock import patch

import pytest

from auth.token_store import TokenStore


class TestSaveAndLoad:
    def test_round_trip(self, tmp_path):
        store = TokenStore("test", cache_dir=tmp_path)
        data = {
            "access_token": "tok123",
            "refresh_token": "ref456",
            "expires_at": time.time() + 3600,
        }
        store.save_token(data)
        loaded = store.get_token()
        assert loaded["access_token"] == "tok123"
        assert loaded["refresh_token"] == "ref456"

    def test_returns_none_if_missing(self, tmp_path):
        store = TokenStore("nope", cache_dir=tmp_path)
        assert store.get_token() is None

    def test_returns_none_if_expired(self, tmp_path):
        store = TokenStore("expired", cache_dir=tmp_path)
        store.save_token({
            "access_token": "old",
            "expires_at": time.time() - 100,
        })
        assert store.get_token() is None

    def test_returns_token_without_expiry(self, tmp_path):
        """Tokens without expires_at are assumed valid."""
        store = TokenStore("noexp", cache_dir=tmp_path)
        store.save_token({"access_token": "forever"})
        loaded = store.get_token()
        assert loaded["access_token"] == "forever"


class TestFilePermissions:
    def test_file_is_0600(self, tmp_path):
        store = TokenStore("perms", cache_dir=tmp_path)
        store.save_token({"access_token": "x"})
        mode = store.cache_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_dir_is_0700(self, tmp_path):
        cache_dir = tmp_path / "new_dir"
        store = TokenStore("perms", cache_dir=cache_dir)
        store.save_token({"access_token": "x"})
        mode = cache_dir.stat().st_mode & 0o777
        assert mode == 0o700

    def test_atomic_overwrite_tightens_perms(self, tmp_path):
        """If the cache file already exists with broader perms (e.g. from a
        previous insecure write), save_token must replace it atomically with
        a 0600 file — no window where the old broader perms remain valid."""
        store = TokenStore("legacy", cache_dir=tmp_path)
        # Simulate an old, world-readable cache file
        store.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        store.cache_file.write_text('{"access_token": "old"}')
        store.cache_file.chmod(0o644)
        assert store.cache_file.stat().st_mode & 0o777 == 0o644

        store.save_token({"access_token": "new"})

        assert store.cache_file.stat().st_mode & 0o777 == 0o600
        assert store.get_token()["access_token"] == "new"

    def test_temp_files_cleaned_up(self, tmp_path):
        """Atomic-write tempfiles should not linger after save_token."""
        store = TokenStore("cleanup", cache_dir=tmp_path)
        store.save_token({"access_token": "x"})
        tmp_leftovers = list(tmp_path.glob(".*.tmp"))
        assert tmp_leftovers == [], f"leftover tempfiles: {tmp_leftovers}"

    def test_temp_file_cleaned_up_on_replace_failure(self, tmp_path):
        """If os.replace fails, the tempfile must still be cleaned up to
        avoid leaving stray .tmp files with secrets in the cache dir."""
        store = TokenStore("replace_fail", cache_dir=tmp_path)

        with patch("auth.token_store.os.replace",
                   side_effect=OSError("simulated rename failure")):
            with pytest.raises(OSError, match="simulated"):
                store.save_token({"access_token": "secret"})

        leftovers = list(tmp_path.glob(".replace_fail.*.tmp"))
        assert leftovers == [], (
            f"leftover tempfile(s) after failed save: {leftovers}"
        )
        # And the target file should not exist (write never completed)
        assert not store.cache_file.exists()


class TestClear:
    def test_clear_removes_file(self, tmp_path):
        store = TokenStore("clear", cache_dir=tmp_path)
        store.save_token({"access_token": "x"})
        assert store.cache_file.exists()
        store.clear()
        assert not store.cache_file.exists()

    def test_clear_no_error_if_missing(self, tmp_path):
        store = TokenStore("missing", cache_dir=tmp_path)
        store.clear()  # should not raise


class TestRefresh:
    def test_refresh_saves_new_token(self, tmp_path):
        store = TokenStore("refresh", cache_dir=tmp_path)
        store.save_token({
            "access_token": "old",
            "refresh_token": "ref123",
            "expires_at": time.time() - 100,  # expired
        })

        import urllib.request
        import io

        def fake_urlopen(req, timeout=None):
            from unittest.mock import MagicMock
            resp = MagicMock()
            resp.read.return_value = json.dumps({
                "access_token": "new_tok",
                "refresh_token": "ref456",
                "expires_in": 3600,
            }).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.token_store.urllib.request.urlopen",
                   side_effect=fake_urlopen):
            token = store.refresh_if_needed("cid", "secret", "https://token.url")

        assert token == "new_tok"
        saved = store.get_token()
        assert saved["access_token"] == "new_tok"
        assert saved["refresh_token"] == "ref456"

    def test_refresh_returns_none_without_refresh_token(self, tmp_path):
        store = TokenStore("noref", cache_dir=tmp_path)
        store.save_token({
            "access_token": "old",
            "expires_at": time.time() - 100,
        })
        result = store.refresh_if_needed("cid", "secret", "https://token.url")
        assert result is None

    def test_returns_valid_token_without_refresh(self, tmp_path):
        """If token is still valid, return it without refreshing."""
        store = TokenStore("valid", cache_dir=tmp_path)
        store.save_token({
            "access_token": "still_good",
            "expires_at": time.time() + 3600,
        })
        token = store.refresh_if_needed("cid", "secret", "https://token.url")
        assert token == "still_good"

    def test_refresh_uses_form_encoded_body(self, tmp_path):
        """Per RFC 6749 §4.1.3 the token endpoint request must be
        application/x-www-form-urlencoded. GitHub specifically requires this."""
        store = TokenStore("refresh_fmt", cache_dir=tmp_path)
        store.save_token({
            "access_token": "old",
            "refresh_token": "ref123",
            "expires_at": time.time() - 100,
        })

        captured = {}

        def fake_urlopen(req, timeout=None):
            from unittest.mock import MagicMock
            captured["body"] = req.data
            captured["content_type"] = req.headers.get("Content-type")
            captured["accept"] = req.headers.get("Accept")
            resp = MagicMock()
            resp.read.return_value = json.dumps({
                "access_token": "new_tok",
                "expires_in": 3600,
            }).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("auth.token_store.urllib.request.urlopen",
                   side_effect=fake_urlopen):
            store.refresh_if_needed("cid", "secret", "https://token.url")

        assert captured["content_type"] == "application/x-www-form-urlencoded"
        assert captured["accept"] == "application/json"
        # Form-encoded body should contain key=value pairs joined with &
        body_str = captured["body"].decode()
        assert "grant_type=refresh_token" in body_str
        assert "client_id=cid" in body_str
        assert "refresh_token=ref123" in body_str


class TestDefaults:
    """Pin the production default cache location so it can't silently regress."""

    def test_default_cache_dir(self):
        from pathlib import Path
        from auth.token_store import DEFAULT_CACHE_DIR
        assert DEFAULT_CACHE_DIR == Path.home() / ".bond_mcps"
