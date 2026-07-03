"""Tests for the email → Cognito sub resolver used by token exchange.

boto3 is not a test-time dependency (it's imported lazily and only when a
user pool is configured), so these tests inject a fake ``boto3`` module into
``sys.modules`` to exercise the ListUsers path without the real SDK.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from auth.auth_server import cognito_lookup


@pytest.fixture(autouse=True)
def _clear_cache():
    cognito_lookup.reset_cache_for_tests()
    yield
    cognito_lookup.reset_cache_for_tests()


def _install_fake_boto3(monkeypatch, list_users_impl) -> MagicMock:
    """Insert a fake ``boto3`` module whose client.list_users is mocked."""
    fake_client = MagicMock()
    fake_client.list_users.side_effect = list_users_impl
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = MagicMock(return_value=fake_client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    return fake_client


def _users_response(sub: str):
    return {"Users": [{"Username": "u1", "Attributes": [{"Name": "sub", "Value": sub}]}]}


class TestPassthrough:
    def test_passthrough_when_pool_unset_on_sqlite(self, monkeypatch):
        monkeypatch.delenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, raising=False)
        monkeypatch.setenv("BOND_MCPS_DB_URL", "sqlite:///./tokens.db")
        assert cognito_lookup.resolve_cognito_sub("Alice@Example.com") == "alice@example.com"

    def test_passthrough_refused_on_postgres(self, monkeypatch):
        monkeypatch.delenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, raising=False)
        monkeypatch.setenv("BOND_MCPS_DB_URL", "postgresql://u:p@h:5432/d?sslmode=require")
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") is None


class TestBoto3Lookup:
    def test_resolves_sub_from_cognito(self, monkeypatch):
        monkeypatch.setenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, "us-east-1_pool")
        monkeypatch.setenv(cognito_lookup.ENV_COGNITO_REGION, "us-east-1")
        client = _install_fake_boto3(monkeypatch, lambda **kw: _users_response("cognito-sub-123"))
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") == "cognito-sub-123"
        # Filter is built from the (lowercased) email.
        _, kwargs = client.list_users.call_args
        assert kwargs["Filter"] == 'email = "alice@example.com"'
        assert kwargs["UserPoolId"] == "us-east-1_pool"

    def test_no_user_found_returns_none(self, monkeypatch):
        monkeypatch.setenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, "pool")
        _install_fake_boto3(monkeypatch, lambda **kw: {"Users": []})
        assert cognito_lookup.resolve_cognito_sub("ghost@example.com") is None

    def test_client_error_returns_none(self, monkeypatch):
        monkeypatch.setenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, "pool")

        def boom(**kw):
            raise RuntimeError("throttled")

        _install_fake_boto3(monkeypatch, boom)
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") is None


class TestCache:
    def test_positive_cache_hit_avoids_second_lookup(self, monkeypatch):
        monkeypatch.setenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, "pool")
        client = _install_fake_boto3(monkeypatch, lambda **kw: _users_response("sub-a"))
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") == "sub-a"
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") == "sub-a"
        assert client.list_users.call_count == 1

    def test_negative_cache_hit_avoids_second_lookup(self, monkeypatch):
        monkeypatch.setenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, "pool")
        client = _install_fake_boto3(monkeypatch, lambda **kw: {"Users": []})
        assert cognito_lookup.resolve_cognito_sub("ghost@example.com") is None
        assert cognito_lookup.resolve_cognito_sub("ghost@example.com") is None
        assert client.list_users.call_count == 1

    def test_reset_clears_cache(self, monkeypatch):
        monkeypatch.setenv(cognito_lookup.ENV_COGNITO_USER_POOL_ID, "pool")
        client = _install_fake_boto3(monkeypatch, lambda **kw: _users_response("sub-a"))
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") == "sub-a"
        cognito_lookup.reset_cache_for_tests()
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") == "sub-a"
        assert client.list_users.call_count == 2
