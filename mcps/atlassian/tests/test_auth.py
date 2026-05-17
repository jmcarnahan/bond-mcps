"""Tests for auth module — token and cloud_id resolution (header + local fallback)."""

import base64
import os

import pytest
from unittest.mock import patch

FAKE_BASIC_AUTH = base64.b64encode(b"user:pass").decode()

# Patch target for get_http_headers -- imported lazily inside functions,
# so we patch at the source module.
_HEADERS_PATCH = "fastmcp.server.dependencies.get_http_headers"


class TestGetAtlassianToken:
    """Test Bearer token extraction (Path 1)."""

    def test_extracts_token_from_lowercase_header(self):
        from atlassian.auth import get_atlassian_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer test-token-123"}):
            assert get_atlassian_token() == "test-token-123"

    def test_calls_get_http_headers_with_authorization_include(self):
        from atlassian.auth import get_atlassian_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer tok"}) as mock_fn:
            get_atlassian_token()
        mock_fn.assert_called_once_with(include={"authorization"})

    def test_raises_if_no_auth_header(self):
        from atlassian.auth import get_atlassian_token

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_atlassian_token()

    def test_raises_if_not_bearer(self):
        from atlassian.auth import get_atlassian_token

        with patch(_HEADERS_PATCH, return_value={"authorization": f"Basic {FAKE_BASIC_AUTH}"}), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Authorization required"):
                get_atlassian_token()


class TestGetAtlassianTokenFallback:
    """Test the fallback paths when no Bearer header is present."""

    def test_falls_back_to_local_auth(self):
        from atlassian.auth import get_atlassian_token

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {"ATLASSIAN_CLIENT_ID": "test"}), \
             patch("atlassian.local_auth.get_local_token_and_cloud_id",
                   return_value=("local-tok", "cloud-123")):
            assert get_atlassian_token() == "local-tok"

    def test_bearer_takes_priority(self):
        from atlassian.auth import get_atlassian_token

        with patch(_HEADERS_PATCH, return_value={"authorization": "Bearer header-tok"}), \
             patch.dict(os.environ, {"ATLASSIAN_CLIENT_ID": "test"}):
            assert get_atlassian_token() == "header-tok"


class TestGetCloudId:
    """Test cloud_id resolution."""

    def test_extracts_cloud_id_from_header(self):
        from atlassian.auth import get_cloud_id

        with patch(_HEADERS_PATCH, return_value={"x-atlassian-cloud-id": "abc-123"}):
            assert get_cloud_id() == "abc-123"

    def test_calls_get_http_headers_with_cloud_id_include(self):
        from atlassian.auth import get_cloud_id

        with patch(_HEADERS_PATCH, return_value={"x-atlassian-cloud-id": "abc"}) as mock_fn:
            get_cloud_id()
        mock_fn.assert_called_once_with(include={"x-atlassian-cloud-id"})

    def test_falls_back_to_env_var(self):
        from atlassian.auth import get_cloud_id

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {"ATLASSIAN_CLOUD_ID": "env-cloud-id"}):
            assert get_cloud_id() == "env-cloud-id"

    def test_falls_back_to_local_auth(self):
        from atlassian.auth import get_cloud_id

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {"ATLASSIAN_CLIENT_ID": "test"}, clear=True), \
             patch("atlassian.local_auth.get_local_token_and_cloud_id",
                   return_value=("tok", "discovered-cloud")):
            assert get_cloud_id() == "discovered-cloud"

    def test_raises_when_no_source(self):
        from atlassian.auth import get_cloud_id

        with patch(_HEADERS_PATCH, return_value={}), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(PermissionError, match="Cloud ID required"):
                get_cloud_id()

    def test_header_takes_priority(self):
        from atlassian.auth import get_cloud_id

        with patch(_HEADERS_PATCH, return_value={"x-atlassian-cloud-id": "header-cloud"}), \
             patch.dict(os.environ, {"ATLASSIAN_CLOUD_ID": "env-cloud"}):
            assert get_cloud_id() == "header-cloud"
