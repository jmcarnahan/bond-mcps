"""Tests for the MCP registry endpoint."""

import json
import os
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from auth.auth_server.endpoints import build_app


@pytest.fixture
def client():
    return TestClient(build_app(), raise_server_exceptions=False)


class TestRegistryEndpoint:
    def test_returns_config_from_env(self, client):
        config = {
            "mcpServers": {
                "github": {"url": "http://github:8000/github/mcp", "display_name": "GitHub"}
            }
        }
        with patch.dict(os.environ, {"BOND_MCPS_REGISTRY_CONFIG": json.dumps(config)}):
            resp = client.get("/registry")
        assert resp.status_code == 200
        assert resp.json() == config

    def test_returns_empty_when_env_unset(self, client):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOND_MCPS_REGISTRY_CONFIG", None)
            resp = client.get("/registry")
        assert resp.status_code == 200
        assert resp.json() == {"mcpServers": {}}

    def test_content_type_is_json(self, client):
        with patch.dict(os.environ, {"BOND_MCPS_REGISTRY_CONFIG": '{"mcpServers":{}}'}):
            resp = client.get("/registry")
        assert "application/json" in resp.headers["content-type"]

    def test_returns_empty_on_malformed_json(self, client):
        with patch.dict(os.environ, {"BOND_MCPS_REGISTRY_CONFIG": "not valid json {"}):
            resp = client.get("/registry")
        assert resp.status_code == 200
        assert resp.json() == {"mcpServers": {}}

    def test_returns_empty_on_whitespace_only(self, client):
        with patch.dict(os.environ, {"BOND_MCPS_REGISTRY_CONFIG": "   "}):
            resp = client.get("/registry")
        assert resp.status_code == 200
        assert resp.json() == {"mcpServers": {}}

    def test_full_config_structure(self, client):
        config = {
            "mcpServers": {
                "microsoft": {
                    "url": "http://ms-graph.test-ns.svc.cluster.local:8000/ms-graph/mcp",
                    "transport": "streamable-http",
                    "auth_type": "external",
                    "display_name": "Microsoft",
                    "description": "Connect to Microsoft email, Teams, OneDrive, and SharePoint",
                    "connect_provider": "microsoft",
                    "connect_url_base": "https://test.example.com/ms-graph",
                    "internal_url_base": "http://ms-graph.test-ns.svc.cluster.local:8000/ms-graph",
                    "jwt_audience": "ms-graph",
                    "pass_jwt": True,
                    "allowed_tools": ["get_user_profile", "list_emails"],
                },
                "grafana": {
                    "url": "http://grafana.test-ns.svc.cluster.local:8000/grafana/mcp",
                    "transport": "streamable-http",
                    "auth_type": "external",
                    "display_name": "Grafana",
                    "description": "Logs, traces, metrics",
                    "internal_url_base": "http://grafana.test-ns.svc.cluster.local:8000/grafana",
                    "jwt_audience": "grafana",
                    "pass_jwt": True,
                },
            }
        }
        with patch.dict(os.environ, {"BOND_MCPS_REGISTRY_CONFIG": json.dumps(config)}):
            resp = client.get("/registry")
        data = resp.json()
        ms = data["mcpServers"]["microsoft"]
        assert ms["allowed_tools"] == ["get_user_profile", "list_emails"]
        assert ms["connect_provider"] == "microsoft"
        grafana = data["mcpServers"]["grafana"]
        assert "connect_provider" not in grafana
        assert "allowed_tools" not in grafana

    def test_entry_without_allowed_tools_exposes_all(self, client):
        """Services without allowed_tools expose all tools via tools/list — the
        registry simply omits the field and the consumer shows everything."""
        config = {
            "mcpServers": {
                "atlassian": {
                    "url": "http://atlassian.ns.svc.cluster.local:8000/atlassian/mcp",
                    "transport": "streamable-http",
                    "auth_type": "external",
                    "display_name": "Atlassian",
                    "description": "Jira and Confluence",
                    "connect_provider": "atlassian",
                    "connect_url_base": "https://test.example.com/atlassian",
                    "internal_url_base": "http://atlassian.ns.svc.cluster.local:8000/atlassian",
                    "jwt_audience": "atlassian",
                    "pass_jwt": True,
                },
            }
        }
        with patch.dict(os.environ, {"BOND_MCPS_REGISTRY_CONFIG": json.dumps(config)}):
            resp = client.get("/registry")
        data = resp.json()
        atlassian = data["mcpServers"]["atlassian"]
        assert "allowed_tools" not in atlassian
        assert atlassian["display_name"] == "Atlassian"
        assert atlassian["connect_provider"] == "atlassian"
