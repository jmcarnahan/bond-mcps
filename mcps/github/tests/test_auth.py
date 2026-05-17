"""Tests for auth.py -- token resolution (Bearer header + local auth fallback)."""

import base64
import os

import pytest
from unittest.mock import patch

FAKE_BASIC_AUTH = base64.b64encode(b"user:pass").decode()

# Patch target for get_http_headers -- imported lazily inside get_github_token(),
# so we patch at the source module in fastmcp.
_HEADERS_PATCH = "fastmcp.server.dependencies.get_http_headers"


class TestGetGitHubToken:
    """Test get_github_token() header parsing."""

    def test_extracts_bearer_token(self):
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer ghp_abc123"}):
            token = get_github_token()
        assert token == "ghp_abc123"

    def test_calls_get_http_headers_with_authorization_include(self):
        """Verify get_http_headers is called with include={'authorization'}."""
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer tok"}) as mock_headers:
            get_github_token()
        mock_headers.assert_called_once_with(include={"authorization"})

    def test_raises_on_missing_header(self):
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, return_value={}):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_github_token()

    def test_raises_on_empty_authorization(self):
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, return_value={"authorization": ""}):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_github_token()

    def test_raises_on_non_bearer_scheme(self):
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, return_value={"authorization": f"Basic {FAKE_BASIC_AUTH}"}):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_github_token()

    def test_preserves_full_token_value(self):
        """Token may contain dots, underscores, etc. -- ensure nothing is stripped."""
        from github.auth import get_github_token

        long_token = "test_token_with.dots_and-dashes.1234"
        with patch(_HEADERS_PATCH, return_value={"authorization": f"Bearer {long_token}"}):
            token = get_github_token()
        assert token == long_token


class TestGetGitHubTokenFallback:
    """Test fallback to local auth when no Bearer header."""

    def test_falls_back_to_local_auth(self):
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, side_effect=Exception("no HTTP context")), \
             patch.dict(os.environ, {"GITHUB_CLIENT_ID": "test-id"}), \
             patch("github.local_auth.get_local_token", return_value="local-tok"):
            token = get_github_token()
        assert token == "local-tok"

    def test_raises_when_no_header_and_no_client_id(self):
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, side_effect=Exception("no HTTP context")), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_github_token()

    def test_bearer_header_takes_priority_over_local_auth(self):
        from github.auth import get_github_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer header-tok"}), \
             patch.dict(os.environ, {"GITHUB_CLIENT_ID": "test-id"}):
            token = get_github_token()
        assert token == "header-tok"
