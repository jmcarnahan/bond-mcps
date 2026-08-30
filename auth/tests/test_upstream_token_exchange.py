"""Tests for auth.auth_server.upstream — verifies correct client credential placement.

Confidential clients (with client_secret) must send credentials via HTTP Basic auth
only. Public clients (no secret) must send client_id in the POST body only. Sending
both simultaneously causes Okta (and other strict OIDC providers) to reject the request.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def _upstream_env(monkeypatch):
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_IDP", "okta")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_ISSUER", "https://example.okta.com/oauth2/default")
    monkeypatch.setenv(
        "BOND_MCPS_UPSTREAM_REDIRECT_URI", "http://localhost:8001/oauth/upstream/callback"
    )
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_SCOPES", "openid email")


class TestConfidentialClient:
    """When client_secret is set, credentials go via HTTP Basic auth only."""

    def test_client_id_not_in_post_body(self, _upstream_env, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_ID", "my-client-id")
        monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_SECRET", "my-secret")

        from auth.auth_server.upstream import get_upstream_idp

        idp = get_upstream_idp()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id_token": "eyJ.eyJzdWIiOiJ1c2VyMSIsImVtYWlsIjoiYUBiLmMifQ.sig",
            "access_token": "at-123",
        }

        mock_meta = {
            "authorization_endpoint": "https://example.okta.com/authorize",
            "token_endpoint": "https://example.okta.com/token",
        }

        with patch.object(idp, "_meta", return_value=mock_meta):
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.post.return_value = mock_response
                mock_client_cls.return_value = mock_client

                with patch(
                    "auth.auth_server.upstream._decode_id_token_claims_unsafe",
                    return_value={"sub": "user1", "email": "a@b.c"},
                ):
                    idp.exchange_code(code="auth-code", code_verifier="verifier123")

                call_kwargs = mock_client.post.call_args
                post_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data", {})
                post_auth = call_kwargs.kwargs.get("auth") or call_kwargs[1].get("auth")

                # Credentials sent via Basic auth
                assert post_auth == ("my-client-id", "my-secret")
                # client_id NOT in POST body
                assert "client_id" not in post_data


class TestPublicClient:
    """When client_secret is empty, client_id goes in POST body with no Basic auth."""

    def test_client_id_in_post_body_no_basic_auth(self, _upstream_env, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_ID", "public-client-id")
        monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_SECRET", "")

        from auth.auth_server.upstream import get_upstream_idp

        idp = get_upstream_idp()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id_token": "eyJ.eyJzdWIiOiJ1c2VyMiIsImVtYWlsIjoieEB5LnoifQ.sig",
            "access_token": "at-456",
        }

        mock_meta = {
            "authorization_endpoint": "https://example.okta.com/authorize",
            "token_endpoint": "https://example.okta.com/token",
        }

        with patch.object(idp, "_meta", return_value=mock_meta):
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.post.return_value = mock_response
                mock_client_cls.return_value = mock_client

                with patch(
                    "auth.auth_server.upstream._decode_id_token_claims_unsafe",
                    return_value={"sub": "user2", "email": "x@y.z"},
                ):
                    idp.exchange_code(code="auth-code", code_verifier="verifier456")

                call_kwargs = mock_client.post.call_args
                post_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data", {})
                post_auth = call_kwargs.kwargs.get("auth") or call_kwargs[1].get("auth")

                # No Basic auth
                assert post_auth is None
                # client_id IS in POST body
                assert post_data["client_id"] == "public-client-id"
