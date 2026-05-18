"""Integration tests for the FastMCP Bearer header → SQL connector handoff.

These exist because all other tests mock at the `dbx.client` boundary and so
cannot catch a regression in the cross-async-sync-thread auth chain — the
exact failure mode that would silently break Bond AI multi-tenant requests.

The full chain under test:
  FastMCP request handler (async, contextvar alive)
    → databricks_mcp tool wrapper
        → db.resolve_token_now()              [SYNC — must read contextvar HERE]
        → asyncio.to_thread(db.run_query, query, token=<captured>)
            → db._connect(token=<captured>)
                → sql.connect(access_token=<captured>)

The invariants:
  1. The Bearer header reaches sql.connect verbatim.
  2. sql.connect is called with NO credentials_provider — there must not be
     any callback that could later fire from a worker thread with a stale
     (or wrong-tenant) context.
  3. enable_telemetry=False — disabling the connector's daemon thread is the
     belt-and-suspenders fix for the same class of bug.
"""

from unittest.mock import MagicMock, patch


WORKSPACE_HOST = "https://dbc-test.cloud.databricks.com"
HTTP_PATH = "/sql/1.0/warehouses/abc"


def _mock_conn():
    cursor = MagicMock()
    cursor.fetchmany.return_value = []
    cursor.description = []
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


async def test_bearer_header_reaches_sql_connect(monkeypatch):
    """End-to-end: a Bearer header carried in the FastMCP request must arrive
    at sql.connect as `access_token=<that bearer>` — proving the contextvar
    capture in resolve_token_now() is on the live path, not bypassed."""
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
    # Set a PAT to make the failure mode obvious if Bearer capture regressed:
    # we'd see the PAT come through instead of the Bearer.
    monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "FALLBACK-pat-leak-canary")

    from fastmcp import Client
    from databricks_mcp import mcp

    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return _mock_conn()

    # The in-process Client does not produce a real HTTP request, so
    # get_http_headers() returns nothing. We patch it to simulate the Bond AI
    # backend forwarding a Bearer token.
    with patch("fastmcp.server.dependencies.get_http_headers",
               return_value={"authorization": "Bearer USER-A-bearer-token"}), \
         patch("dbx.client.sql.connect", side_effect=fake_connect):
        async with Client(mcp) as client:
            await client.call_tool("run_query", {"query": "SELECT 1"})

    assert captured.get("access_token") == "USER-A-bearer-token", \
        "Bearer header was NOT passed through — the PAT fallback was used " \
        "instead, indicating contextvar capture regressed."
    assert "credentials_provider" not in captured, \
        "credentials_provider callback present — could fire from worker " \
        "thread with stale context."
    # BOTH telemetry flags required: force_enable_telemetry=True overrides
    # enable_telemetry=False (telemetry_client.py:is_telemetry_enabled).
    assert captured.get("enable_telemetry") is False, \
        "Telemetry enabled — background thread could leak auth identity."
    assert captured.get("force_enable_telemetry") is False, \
        "force_enable_telemetry not set False — a config layer that flips " \
        "it on would silently re-enable the leaking daemon thread."


async def test_pat_used_when_no_bearer_header(monkeypatch):
    """Fallback path: no Bearer in the request → PAT env var wins."""
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
    monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "the-pat")

    from fastmcp import Client
    from databricks_mcp import mcp

    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return _mock_conn()

    with patch("fastmcp.server.dependencies.get_http_headers",
               side_effect=Exception("no http context")), \
         patch("dbx.client.sql.connect", side_effect=fake_connect):
        async with Client(mcp) as client:
            await client.call_tool("run_query", {"query": "SELECT 1"})

    assert captured.get("access_token") == "the-pat"


async def test_oauth_wins_over_pat(monkeypatch):
    """If both DATABRICKS_CLIENT_ID and DATABRICKS_ACCESS_TOKEN are set, the
    OAuth path takes precedence — the PAT must not leak through."""
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "oauth-id")
    monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-should-be-ignored")

    from fastmcp import Client
    from databricks_mcp import mcp

    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return _mock_conn()

    with patch("fastmcp.server.dependencies.get_http_headers",
               side_effect=Exception("no http context")), \
         patch("dbx.local_auth.get_local_token", return_value="oauth-tok"), \
         patch("dbx.client.sql.connect", side_effect=fake_connect):
        async with Client(mcp) as client:
            await client.call_tool("run_query", {"query": "SELECT 1"})

    assert captured.get("access_token") == "oauth-tok"
