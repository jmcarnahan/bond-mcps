"""Tests for auth.py -- token resolution (Bearer header + local auth fallback)."""

import base64
import os

import pytest
from unittest.mock import patch

FAKE_BASIC_AUTH = base64.b64encode(b"user:pass").decode()

# Patch target for get_http_headers -- imported lazily inside get_graph_token(),
# so we patch at the source module.
_HEADERS_PATCH = "fastmcp.server.dependencies.get_http_headers"


class TestGetGraphToken:
    """Test get_graph_token() header parsing (Path 1: Bearer header)."""

    def test_extracts_bearer_token(self):
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer my-token-123"}):
            token = get_graph_token()
        assert token == "my-token-123"

    def test_calls_get_http_headers_with_authorization_include(self):
        """Verify get_http_headers is called with include={'authorization'}.

        FastMCP v3 strips the authorization header by default.
        Without include={'authorization'}, auth will silently fail.
        """
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer tok"}) as mock_headers:
            get_graph_token()
        mock_headers.assert_called_once_with(include={"authorization"})

    def test_raises_on_missing_header(self):
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_graph_token()

    def test_raises_on_empty_authorization(self):
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, return_value={"authorization": ""}), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_graph_token()

    def test_raises_on_non_bearer_scheme(self):
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, return_value={"authorization": f"Basic {FAKE_BASIC_AUTH}"}), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_graph_token()

    def test_preserves_full_token_value(self):
        """Token may contain dots, slashes, etc. -- ensure nothing is stripped."""
        from ms_graph.auth import get_graph_token

        long_token = "eyJ0eXAi.eyJhdWQi.signature_here"
        with patch(_HEADERS_PATCH, return_value={"authorization": f"Bearer {long_token}"}):
            token = get_graph_token()
        assert token == long_token


class TestGetGraphTokenFallback:
    """Test the fallback paths when no Bearer header is present."""

    def test_falls_back_to_local_auth_when_no_header_and_client_id_set(self):
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}), \
             patch("ms_graph.local_auth.get_local_token", return_value="local-tok"):
            token = get_graph_token()
        assert token == "local-tok"

    def test_raises_when_no_header_and_no_client_id(self):
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_graph_token()

    def test_bearer_header_takes_priority_over_local_auth(self):
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH,
                   return_value={"authorization": "Bearer header-tok"}), \
             patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}):
            token = get_graph_token()
        assert token == "header-tok"

    def test_falls_back_when_get_http_headers_raises(self):
        """When get_http_headers raises (e.g., not in HTTP context), fall back."""
        from ms_graph.auth import get_graph_token

        with patch(_HEADERS_PATCH, side_effect=RuntimeError("no context")), \
             patch.dict(os.environ, {"MS_CLIENT_ID": "test-id"}), \
             patch("ms_graph.local_auth.get_local_token", return_value="local-tok"):
            token = get_graph_token()
        assert token == "local-tok"
