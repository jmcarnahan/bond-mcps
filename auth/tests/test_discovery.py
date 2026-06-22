"""Tests for auth.discovery and the /connections/discovery proxy route."""

import http.client
import json
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from auth.discovery import discover_mcps
from auth.proxy_server import AuthProxyHandler, _lock, _pending

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_mcp(mcps_dir: Path, dirname: str, manifest: dict | str) -> Path:
    """Create mcps_dir/<dirname>/mcp.json. ``manifest`` may be a dict or raw str."""
    d = mcps_dir / dirname
    d.mkdir(parents=True, exist_ok=True)
    body = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (d / "mcp.json").write_text(body, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Unit tests: discover_mcps()
# ---------------------------------------------------------------------------


class TestDiscoverMcps:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert discover_mcps(mcps_dir=tmp_path / "does-not-exist") == []

    def test_empty_dir_returns_empty(self, tmp_path):
        assert discover_mcps(mcps_dir=tmp_path) == []

    def test_single_valid_manifest(self, tmp_path):
        _write_mcp(
            tmp_path,
            "microsoft",
            {"name": "ms-graph", "display_name": "Microsoft", "port": 18001, "path": "/mcp"},
        )
        result = discover_mcps(mcps_dir=tmp_path)
        assert result == [
            {
                "name": "ms-graph",
                "display_name": "Microsoft",
                "url": "http://localhost:18001/mcp",
            }
        ]

    def test_multiple_manifests_sorted_by_name(self, tmp_path):
        _write_mcp(tmp_path, "z_micro", {"name": "ms-graph", "port": 18001})
        _write_mcp(tmp_path, "a_git", {"name": "github", "port": 18002})
        _write_mcp(tmp_path, "m_atl", {"name": "atlassian", "port": 18003})
        names = [e["name"] for e in discover_mcps(mcps_dir=tmp_path)]
        assert names == ["atlassian", "github", "ms-graph"]

    def test_dir_without_manifest_skipped(self, tmp_path):
        (tmp_path / "not-an-mcp").mkdir()
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["github"]

    def test_hidden_dir_skipped(self, tmp_path):
        _write_mcp(tmp_path, ".venv", {"name": "sneaky", "port": 9})
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["github"]

    def test_malformed_json_skipped_others_kept(self, tmp_path):
        _write_mcp(tmp_path, "broken", "{not valid json")
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["github"]

    def test_missing_port_skipped(self, tmp_path):
        _write_mcp(tmp_path, "noport", {"name": "noport"})
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["github"]

    def test_missing_name_skipped(self, tmp_path):
        _write_mcp(tmp_path, "noname", {"port": 1234})
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["github"]

    def test_bool_port_rejected(self, tmp_path):
        # JSON true parses to a Python bool, which is an int subclass — reject it.
        _write_mcp(tmp_path, "boolport", {"name": "boolport", "port": True})
        assert discover_mcps(mcps_dir=tmp_path) == []

    def test_default_path_when_omitted(self, tmp_path):
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert discover_mcps(mcps_dir=tmp_path)[0]["url"] == "http://localhost:18002/mcp"

    def test_custom_path_honored(self, tmp_path):
        _write_mcp(tmp_path, "custom", {"name": "custom", "port": 7000, "path": "/api/mcp"})
        assert discover_mcps(mcps_dir=tmp_path)[0]["url"] == "http://localhost:7000/api/mcp"

    def test_invalid_path_skipped(self, tmp_path):
        _write_mcp(tmp_path, "badpath", {"name": "badpath", "port": 7000, "path": "no-slash"})
        assert discover_mcps(mcps_dir=tmp_path) == []

    def test_display_name_defaults_to_name(self, tmp_path):
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert discover_mcps(mcps_dir=tmp_path)[0]["display_name"] == "github"

    def test_base_host_override(self, tmp_path):
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        result = discover_mcps(mcps_dir=tmp_path, base_host="https://mcps.example.com")
        # host override drops the port-less form: scheme+host : port + path
        assert result[0]["url"] == "https://mcps.example.com:18002/mcp"

    def test_env_override_respected(self, tmp_path, monkeypatch):
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        monkeypatch.setenv("BOND_MCPS_MCPS_DIR", str(tmp_path))
        # No mcps_dir arg → falls back to env-resolved dir.
        assert [e["name"] for e in discover_mcps()] == ["github"]

    def test_dynamic_add_and_remove(self, tmp_path):
        _write_mcp(tmp_path, "github", {"name": "github", "port": 18002})
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["github"]

        # Add → appears with no config change.
        _write_mcp(tmp_path, "atlassian", {"name": "atlassian", "port": 18003})
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["atlassian", "github"]

        # Remove → disappears.
        (tmp_path / "atlassian" / "mcp.json").unlink()
        (tmp_path / "atlassian").rmdir()
        assert [e["name"] for e in discover_mcps(mcps_dir=tmp_path)] == ["github"]


# ---------------------------------------------------------------------------
# Contract test: the real repo mcps/ directory
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_discovery_env(monkeypatch):
    """Keep filesystem/real-repo tests hermetic against ambient deploy env."""
    monkeypatch.delenv("BOND_MCPS_DISCOVERY_FILE", raising=False)
    monkeypatch.delenv("BOND_MCPS_MCPS_DIR", raising=False)


class TestRealRepoManifests:
    def test_known_mcps_present(self):
        """Discovery against the real checkout returns the four shipped MCPs.

        Superset assertion so adding a 5th MCP later doesn't break this test.
        """
        found = {e["name"]: e for e in discover_mcps()}
        expected = {
            "microsoft": "http://localhost:18001/mcp",
            "github": "http://localhost:18002/mcp",
            "atlassian": "http://localhost:18003/mcp",
            "databricks": "http://localhost:18004/mcp",
        }
        for name, url in expected.items():
            assert name in found, f"{name} missing from discovery"
            assert found[name]["url"] == url


# ---------------------------------------------------------------------------
# Integration tests: GET /connections/discovery on the proxy server
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_state():
    with _lock:
        _pending.clear()
    yield
    with _lock:
        _pending.clear()


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), AuthProxyHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, port
    srv.shutdown()
    srv.server_close()


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


class TestDiscoveryRoute:
    def test_discovery_returns_mcps(self, server, tmp_path, monkeypatch):
        _write_mcp(tmp_path, "github", {"name": "github", "display_name": "GitHub", "port": 18002})
        monkeypatch.setenv("BOND_MCPS_MCPS_DIR", str(tmp_path))
        _, port = server

        status, body = _get(port, "/connections/discovery")
        assert status == 200
        data = json.loads(body)
        assert data == {
            "mcps": [
                {
                    "name": "github",
                    "display_name": "GitHub",
                    "url": "http://localhost:18002/mcp",
                }
            ]
        }

    def test_discovery_empty_when_no_mcps(self, server, tmp_path, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_MCPS_DIR", str(tmp_path))
        _, port = server
        status, body = _get(port, "/connections/discovery")
        assert status == 200
        assert json.loads(body) == {"mcps": []}

    def test_discovery_requires_no_auth(self, server, tmp_path, monkeypatch):
        """No Authorization header → still 200 (endpoint is unauthenticated)."""
        monkeypatch.setenv("BOND_MCPS_MCPS_DIR", str(tmp_path))
        _, port = server
        status, _ = _get(port, "/connections/discovery", headers={})
        assert status == 200

    def test_unknown_connections_path_still_404(self, server):
        """Regression guard: discovery branch doesn't swallow other paths."""
        _, port = server
        status, _ = _get(port, "/connections/nope")
        assert status == 404


# ---------------------------------------------------------------------------
# Deployment source: BOND_MCPS_DISCOVERY_FILE (deploy-time JSON manifest)
# ---------------------------------------------------------------------------


def _write_discovery_file(tmp_path, payload) -> str:
    path = tmp_path / "discovery.json"
    body = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestDiscoveryFileSource:
    def test_absolute_urls_used_verbatim(self, tmp_path, monkeypatch):
        f = _write_discovery_file(
            tmp_path,
            {
                "mcps": [
                    {
                        "name": "ms-graph",
                        "display_name": "Microsoft",
                        "url": "https://ms-graph.x/mcp",
                    },
                    {"name": "github", "url": "https://github.x/mcp"},
                ]
            },
        )
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", f)
        result = discover_mcps()
        assert result == [
            {"name": "github", "display_name": "github", "url": "https://github.x/mcp"},
            {"name": "ms-graph", "display_name": "Microsoft", "url": "https://ms-graph.x/mcp"},
        ]

    def test_bare_list_accepted(self, tmp_path, monkeypatch):
        f = _write_discovery_file(tmp_path, [{"name": "github", "url": "https://github.x/mcp"}])
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", f)
        assert [e["name"] for e in discover_mcps()] == ["github"]

    def test_port_form_in_file_uses_base_host(self, tmp_path, monkeypatch):
        f = _write_discovery_file(tmp_path, {"mcps": [{"name": "github", "port": 18002}]})
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", f)
        assert discover_mcps()[0]["url"] == "http://localhost:18002/mcp"

    def test_malformed_entry_skipped(self, tmp_path, monkeypatch):
        f = _write_discovery_file(
            tmp_path,
            {
                "mcps": [
                    {"display_name": "no name"},
                    {"name": "github", "url": "https://github.x/mcp"},
                ]
            },
        )
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", f)
        assert [e["name"] for e in discover_mcps()] == ["github"]

    def test_malformed_file_returns_empty(self, tmp_path, monkeypatch):
        f = _write_discovery_file(tmp_path, "{not valid json")
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", f)
        assert discover_mcps() == []

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", str(tmp_path / "nope.json"))
        assert discover_mcps() == []

    def test_explicit_mcps_dir_overrides_file_env(self, tmp_path, monkeypatch):
        # An explicit mcps_dir arg wins over the ambient file env (test seam).
        f = _write_discovery_file(
            tmp_path, {"mcps": [{"name": "fromfile", "url": "https://x/mcp"}]}
        )
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", f)
        scan_dir = tmp_path / "scan"
        _write_mcp(scan_dir, "github", {"name": "github", "port": 18002})
        assert [e["name"] for e in discover_mcps(mcps_dir=scan_dir)] == ["github"]


# ---------------------------------------------------------------------------
# Authorization Server hosts /connections/discovery in deployment
# ---------------------------------------------------------------------------


class TestAuthServerDiscoveryRoute:
    def test_as_serves_discovery_from_file(self, tmp_path, monkeypatch):
        from starlette.testclient import TestClient

        from auth.auth_server import build_app

        f = _write_discovery_file(
            tmp_path,
            {"mcps": [{"name": "github", "display_name": "GitHub", "url": "https://github.x/mcp"}]},
        )
        monkeypatch.setenv("BOND_MCPS_DISCOVERY_FILE", f)
        client = TestClient(build_app())
        resp = client.get("/connections/discovery")
        assert resp.status_code == 200
        assert resp.json() == {
            "mcps": [{"name": "github", "display_name": "GitHub", "url": "https://github.x/mcp"}]
        }
