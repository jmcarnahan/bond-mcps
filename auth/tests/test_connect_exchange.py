"""Tests for connect_routes._exchange_code outbound request shape."""

import json
from unittest.mock import MagicMock, patch

import pytest

from auth.connect_routes import ProviderConnectConfig, _exchange_code
from auth.token_store import OUTBOUND_USER_AGENT


@pytest.fixture
def config():
    return ProviderConnectConfig(
        name="omnea",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        scopes="openid",
        client_id_env="TEST_CLIENT_ID",
        client_secret_env="TEST_CLIENT_SECRET",
    )


def _run_exchange(config, response_status=200, response_json=None):
    captured = {}
    resp = MagicMock()
    resp.status_code = response_status
    resp.text = json.dumps(response_json or {})
    resp.json.return_value = response_json or {}

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def fake_post(url, data=None, headers=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return resp

    client.post.side_effect = fake_post

    with patch("auth.connect_routes.httpx.Client", return_value=client):
        result = _exchange_code(
            config=config,
            code="code123",
            code_verifier="ver",
            redirect_uri="https://cb.example/connect/omnea/callback",
            client_id="cid",
            client_secret="sec",
        )
    return captured, result


def test_exchange_sends_browser_user_agent(config):
    """Cloudflare-fronted token endpoints (e.g. WorkOS AuthKit) 403-ban
    default Python UAs; the code exchange must send a browser-like UA."""
    captured, _ = _run_exchange(config, response_json={"access_token": "tok"})

    assert captured["headers"]["User-Agent"] == OUTBOUND_USER_AGENT
    assert captured["headers"]["Accept"] == "application/json"


def test_exchange_sends_code_grant_form(config):
    captured, result = _run_exchange(config, response_json={"access_token": "tok"})

    assert captured["url"] == "https://example.com/token"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "code123"
    assert captured["data"]["code_verifier"] == "ver"
    assert result == {"access_token": "tok"}
