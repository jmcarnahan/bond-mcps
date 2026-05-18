"""Tests for dbx/auth.py — three-path token resolution."""

import os
from unittest.mock import patch

import pytest

_HEADERS_PATCH = "fastmcp.server.dependencies.get_http_headers"


class TestBearerHeader:
    def test_extracts_bearer_token(self):
        from dbx.auth import get_databricks_token
        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer dbx-tok-1"}):
            assert get_databricks_token() == "dbx-tok-1"

    def test_calls_get_http_headers_with_authorization_include(self):
        from dbx.auth import get_databricks_token
        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer tok"}) as m:
            get_databricks_token()
        m.assert_called_with(include={"authorization"})

    def test_non_bearer_scheme_skipped(self, monkeypatch):
        from dbx.auth import get_databricks_token
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-fallback")
        with patch(_HEADERS_PATCH, return_value={"authorization": "Basic xxx"}):
            # Bearer header absent, falls through to PAT path
            assert get_databricks_token() == "pat-fallback"

    def test_preserves_token_with_special_chars(self):
        from dbx.auth import get_databricks_token
        long_tok = "eyJraWQ.dot_under-dash.signature123"
        with patch(_HEADERS_PATCH, return_value={"authorization": f"Bearer {long_tok}"}):
            assert get_databricks_token() == long_tok


class TestOAuthPath:
    def test_oauth_invoked_when_client_id_set(self, monkeypatch):
        from dbx.auth import get_databricks_token
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")), \
             patch("dbx.local_auth.get_local_token", return_value="oauth-tok"):
            assert get_databricks_token() == "oauth-tok"

    def test_oauth_wins_over_pat(self, monkeypatch):
        from dbx.auth import get_databricks_token
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-tok")
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")), \
             patch("dbx.local_auth.get_local_token", return_value="oauth-tok"):
            assert get_databricks_token() == "oauth-tok"


class TestPATPath:
    def test_pat_used_when_only_pat_set(self, monkeypatch):
        from dbx.auth import get_databricks_token
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "dapi-1234")
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")):
            assert get_databricks_token() == "dapi-1234"


class TestNoAuth:
    def test_raises_when_nothing_configured(self):
        from dbx.auth import get_databricks_token
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")):
            with pytest.raises(PermissionError, match="authorization required"):
                get_databricks_token()

    def test_error_mentions_all_three_paths(self):
        from dbx.auth import get_databricks_token
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")):
            with pytest.raises(PermissionError) as exc:
                get_databricks_token()
        msg = str(exc.value)
        assert "OAuth" in msg
        assert "PAT" in msg
        assert "Bearer" in msg


class TestAuthSource:
    def test_bearer_source(self):
        from dbx.auth import get_auth_source, AuthSource
        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer xyz"}):
            assert get_auth_source() is AuthSource.BEARER

    def test_oauth_source(self, monkeypatch):
        from dbx.auth import get_auth_source, AuthSource
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")):
            assert get_auth_source() is AuthSource.OAUTH

    def test_pat_source(self, monkeypatch):
        from dbx.auth import get_auth_source, AuthSource
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat")
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")):
            assert get_auth_source() is AuthSource.PAT

    def test_source_raises_when_unconfigured(self):
        from dbx.auth import get_auth_source
        with patch(_HEADERS_PATCH, side_effect=Exception("no http ctx")):
            with pytest.raises(PermissionError):
                get_auth_source()
