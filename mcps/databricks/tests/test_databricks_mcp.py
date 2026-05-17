"""Tests for databricks_mcp.py — FastMCP tools via in-process Client.

The repo's pattern is to call tools through fastmcp.Client(mcp_server) so the
tool wrappers run exactly the same path as the deployed server. We mock at the
dbx.client boundary; the SQL connector is never invoked.
"""

from unittest.mock import patch

import pytest

from tests.conftest import WORKSPACE_HOST, HTTP_PATH, SAMPLE_SELECT_RESULT


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
    monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-test")


@pytest.fixture
def mcp_server():
    from databricks_mcp import mcp
    return mcp


def _text(result) -> str:
    """Extract text from a FastMCP CallToolResult."""
    return result.content[0].text


async def _call(mcp_server, name: str, args: dict | None = None) -> str:
    from fastmcp import Client
    async with Client(mcp_server) as client:
        result = await client.call_tool(name, args or {})
    return _text(result)


class TestRunQuery:
    async def test_returns_table_for_small_result(self, mcp_server):
        with patch("databricks_mcp.db.run_query", return_value=SAMPLE_SELECT_RESULT):
            out = await _call(mcp_server, "run_query", {"query": "SELECT * FROM t"})
        assert "3 row(s)" in out
        assert "alpha" in out
        assert "beta" in out
        assert "|" in out  # pipe-delimited

    async def test_empty_result_message(self, mcp_server):
        with patch("databricks_mcp.db.run_query",
                   return_value={"columns": ["x"], "rows": []}):
            out = await _call(mcp_server, "run_query",
                              {"query": "SELECT x FROM t WHERE 1=0"})
        assert "No rows" in out
        assert "x" in out

    async def test_no_columns_returned(self, mcp_server):
        with patch("databricks_mcp.db.run_query",
                   return_value={"columns": [], "rows": []}):
            out = await _call(mcp_server, "run_query",
                              {"query": "CREATE TABLE foo (a INT)"})
        assert "no result set" in out

    async def test_csv_fallback_for_large_result(self, mcp_server):
        big = {"columns": ["i"], "rows": [[i] for i in range(75)]}
        with patch("databricks_mcp.db.run_query", return_value=big):
            out = await _call(mcp_server, "run_query", {"query": "SELECT i FROM big"})
        assert "75 rows" in out
        assert "first 50" in out
        assert "```csv" in out

    async def test_empty_query_rejected(self, mcp_server):
        out = await _call(mcp_server, "run_query", {"query": "   "})
        assert "required" in out

    async def test_friendly_error_unauthorized_oauth(self, mcp_server, monkeypatch):
        from dbx.client import DatabricksError
        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        with patch("databricks_mcp.db.run_query",
                   side_effect=DatabricksError("401", error_code="Unauthorized")):
            out = await _call(mcp_server, "run_query", {"query": "SELECT 1"})
        assert "make login-databricks" in out

    async def test_friendly_error_unauthorized_pat(self, mcp_server):
        from dbx.client import DatabricksError
        with patch("databricks_mcp.db.run_query",
                   side_effect=DatabricksError("401", error_code="Unauthorized")):
            out = await _call(mcp_server, "run_query", {"query": "SELECT 1"})
        assert "DATABRICKS_ACCESS_TOKEN" in out

    async def test_friendly_error_forbidden(self, mcp_server):
        from dbx.client import DatabricksError
        with patch("databricks_mcp.db.run_query",
                   side_effect=DatabricksError("403", error_code="Forbidden")):
            out = await _call(mcp_server, "run_query", {"query": "SELECT 1"})
        assert "permission denied" in out.lower()

    async def test_friendly_error_sql(self, mcp_server):
        from dbx.client import DatabricksError
        sql_msg = "TABLE_OR_VIEW_NOT_FOUND: missing.thing"
        with patch("databricks_mcp.db.run_query",
                   side_effect=DatabricksError(sql_msg, error_code="SQLError")):
            out = await _call(mcp_server, "run_query",
                              {"query": "SELECT * FROM missing.thing"})
        assert "SQL error" in out
        assert sql_msg in out

    async def test_friendly_error_missing_config(self, mcp_server):
        from dbx.client import DatabricksError
        with patch("databricks_mcp.db.run_query",
                   side_effect=DatabricksError("missing X", error_code="MissingConfig")):
            out = await _call(mcp_server, "run_query", {"query": "SELECT 1"})
        assert "not configured" in out.lower()


class TestListCatalogs:
    async def test_returns_list(self, mcp_server):
        with patch("databricks_mcp.db.list_catalogs",
                   return_value=["main", "samples", "hive_metastore"]):
            out = await _call(mcp_server, "list_catalogs")
        assert "3 catalog(s)" in out
        assert "samples" in out

    async def test_empty(self, mcp_server):
        with patch("databricks_mcp.db.list_catalogs", return_value=[]):
            out = await _call(mcp_server, "list_catalogs")
        assert "No catalogs" in out


class TestListSchemas:
    async def test_requires_catalog(self, mcp_server):
        out = await _call(mcp_server, "list_schemas", {"catalog": ""})
        assert "required" in out

    async def test_returns_list(self, mcp_server):
        with patch("databricks_mcp.db.list_schemas",
                   return_value=["default", "bronze", "silver"]):
            out = await _call(mcp_server, "list_schemas", {"catalog": "main"})
        assert "3 schema(s) in `main`" in out
        assert "bronze" in out


class TestListTables:
    async def test_requires_both(self, mcp_server):
        out = await _call(mcp_server, "list_tables",
                          {"catalog": "main", "schema": ""})
        assert "both required" in out

    async def test_returns_table(self, mcp_server):
        with patch("databricks_mcp.db.list_tables", return_value=[
            {"database": "default", "table": "events", "is_temporary": False},
            {"database": "default", "table": "tmp_join", "is_temporary": True},
        ]):
            out = await _call(mcp_server, "list_tables",
                              {"catalog": "main", "schema": "default"})
        assert "2 table(s) in `main`.`default`" in out
        assert "events" in out
        assert "tmp_join" in out
        assert "yes" in out
