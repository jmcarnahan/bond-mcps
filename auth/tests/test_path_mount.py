"""Tests for auth.path_mount — ASGI path-prefix mounting + RFC 9728/8414 rewrites."""

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def _clean_env(monkeypatch):
    monkeypatch.delenv("BOND_MCPS_ROOT_PATH", raising=False)
    monkeypatch.delenv("BOND_MCPS_JWT_AUDIENCE", raising=False)


@pytest.fixture
def mock_mcp():
    """Minimal FastMCP-like object with an http_app() that returns a Starlette app."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def healthz(request):
        return JSONResponse({"status": "ok"})

    async def well_known(request):
        return JSONResponse({"resource": "test", "authorization_servers": ["https://as.example"]})

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route(
                "/.well-known/oauth-protected-resource/test-audience/mcp",
                well_known,
                methods=["GET"],
            ),
        ]
    )

    mcp = MagicMock()
    mcp.http_app.return_value = app
    return mcp


class TestNoRootPath:
    def test_returns_inner_app_unchanged(self, _clean_env, mock_mcp):
        from auth.path_mount import mount_app

        result = mount_app(mock_mcp)
        assert result is mock_mcp.http_app.return_value

    def test_healthz_at_root(self, _clean_env, mock_mcp):
        from auth.path_mount import mount_app

        app = mount_app(mock_mcp)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestWithRootPath:
    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_ROOT_PATH", "/ms-graph")
        monkeypatch.setenv("BOND_MCPS_JWT_AUDIENCE", "test-audience")

    def test_healthz_at_prefixed_path(self, mock_mcp):
        from auth.path_mount import mount_app

        app = mount_app(mock_mcp)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ms-graph/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_healthz_at_root_returns_404(self, mock_mcp):
        from auth.path_mount import mount_app

        app = mount_app(mock_mcp)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/healthz")
        assert resp.status_code == 404

    def test_well_known_rewrite_from_root_level(self, mock_mcp):
        """RFC 9728: SDK requests /.well-known/oauth-protected-resource/<audience>/mcp at root."""
        from auth.path_mount import mount_app

        app = mount_app(mock_mcp)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/.well-known/oauth-protected-resource/test-audience/mcp")
        assert resp.status_code == 200
        assert resp.json()["resource"] == "test"

    def test_well_known_rewrite_from_mounted_path(self, mock_mcp):
        """SDK requests /<prefix>/.well-known/oauth-protected-resource/mcp (relative to MCP URL)."""
        from auth.path_mount import mount_app

        app = mount_app(mock_mcp)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ms-graph/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 200
        assert resp.json()["resource"] == "test"

    def test_well_known_internal_path_also_works(self, mock_mcp):
        """Direct access to the fully-qualified internal path."""
        from auth.path_mount import mount_app

        app = mount_app(mock_mcp)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ms-graph/.well-known/oauth-protected-resource/test-audience/mcp")
        assert resp.status_code == 200
        assert resp.json()["resource"] == "test"

    def test_audience_defaults_to_root_path_stripped(self, monkeypatch, mock_mcp):
        """When BOND_MCPS_JWT_AUDIENCE is not set, audience defaults to root_path without slash."""
        monkeypatch.delenv("BOND_MCPS_JWT_AUDIENCE", raising=False)
        monkeypatch.setenv("BOND_MCPS_ROOT_PATH", "/ms-graph")

        # Need a mock that has the route for "ms-graph" audience
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def well_known(request):
            return JSONResponse({"resource": "default-audience-test"})

        app = Starlette(
            routes=[
                Route(
                    "/.well-known/oauth-protected-resource/ms-graph/mcp",
                    well_known,
                    methods=["GET"],
                ),
            ]
        )
        mock_mcp.http_app.return_value = app

        from auth.path_mount import mount_app

        result = mount_app(mock_mcp)
        client = TestClient(result, raise_server_exceptions=False)
        resp = client.get("/.well-known/oauth-protected-resource/ms-graph/mcp")
        assert resp.status_code == 200
        assert resp.json()["resource"] == "default-audience-test"
