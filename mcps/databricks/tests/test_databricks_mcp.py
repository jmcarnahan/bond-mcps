"""Tests for databricks_mcp.py — FastMCP tools via in-process Client."""

import asyncio
import threading
import time
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
    return result.content[0].text


async def _call(mcp_server, name: str, args: dict | None = None) -> str:
    from fastmcp import Client
    async with Client(mcp_server) as client:
        result = await client.call_tool(name, args or {})
    return _text(result)


def _result_with(rows, columns=None, truncated=False):
    cols = columns if columns is not None else SAMPLE_SELECT_RESULT["columns"]
    return {"columns": cols, "rows": rows, "truncated": truncated}


class TestRunQuery:
    async def test_returns_table_for_small_result(self, mcp_server):
        result = {**SAMPLE_SELECT_RESULT, "truncated": False}
        with patch("databricks_mcp.db.run_query", return_value=result):
            out = await _call(mcp_server, "run_query", {"query": "SELECT * FROM t"})
        assert "3 row(s)" in out
        assert "alpha" in out
        assert "beta" in out
        assert "|" in out

    async def test_empty_result_message(self, mcp_server):
        with patch("databricks_mcp.db.run_query", return_value=_result_with([], ["x"])):
            out = await _call(mcp_server, "run_query",
                              {"query": "SELECT x FROM t WHERE 1=0"})
        assert "No rows" in out
        assert "x" in out

    async def test_no_columns_returned(self, mcp_server):
        with patch("databricks_mcp.db.run_query", return_value=_result_with([], [])):
            out = await _call(mcp_server, "run_query",
                              {"query": "CREATE TABLE foo (a INT)"})
        assert "no result set" in out

    async def test_preview_truncation_for_large_result(self, mcp_server):
        """When total > preview cap but cursor wasn't capped: 'showing first N'."""
        big = _result_with([[i] for i in range(75)], columns=["i"])
        with patch("databricks_mcp.db.run_query", return_value=big):
            out = await _call(mcp_server, "run_query", {"query": "SELECT i FROM big"})
        assert "showing first 50" in out
        assert "75 row(s)" in out
        # No "+" marker since the cursor wasn't capped.
        assert "75+ row(s)" not in out

    async def test_truncated_flag_message(self, mcp_server):
        """truncated=True must surface BOTH the '+' count marker and the
        'Fetch was capped' note."""
        result = _result_with(
            [[i] for i in range(50)], columns=["i"], truncated=True,
        )
        with patch("databricks_mcp.db.run_query", return_value=result):
            out = await _call(mcp_server, "run_query",
                              {"query": "SELECT i FROM massive_table"})
        assert "50+ row(s)" in out
        assert "Fetch was capped" in out

    async def test_output_layout_is_consistent(self, mcp_server):
        """Small AND large results must share the same shape: header line
        first, then table. Was previously inconsistent (small led with count,
        large led with table)."""
        small = _result_with([[1], [2]], columns=["i"])
        big = _result_with([[i] for i in range(75)], columns=["i"])
        with patch("databricks_mcp.db.run_query", return_value=small):
            small_out = await _call(mcp_server, "run_query", {"query": "q"})
        with patch("databricks_mcp.db.run_query", return_value=big):
            big_out = await _call(mcp_server, "run_query", {"query": "q"})
        # Both start with "<n> row(s)" header line.
        assert small_out.startswith("2 row(s):")
        assert big_out.startswith("75 row(s) — showing first 50:")

    async def test_empty_query_rejected(self, mcp_server):
        out = await _call(mcp_server, "run_query", {"query": "   "})
        assert "required" in out

    async def test_friendly_error_unauthorized_oauth(self, mcp_server, monkeypatch):
        from dbx.client import DatabricksError
        from dbx.auth import AuthSource
        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        with patch("databricks_mcp.db.resolve_token_now",
                   return_value=("tok", AuthSource.OAUTH)), \
             patch("databricks_mcp.db.run_query",
                   side_effect=DatabricksError("401", error_code="Unauthorized")):
            out = await _call(mcp_server, "run_query", {"query": "SELECT 1"})
        assert "make login-databricks" in out

    async def test_friendly_error_unauthorized_pat(self, mcp_server):
        from dbx.client import DatabricksError
        from dbx.auth import AuthSource
        with patch("databricks_mcp.db.resolve_token_now",
                   return_value=("tok", AuthSource.PAT)), \
             patch("databricks_mcp.db.run_query",
                   side_effect=DatabricksError("401", error_code="Unauthorized")):
            out = await _call(mcp_server, "run_query", {"query": "SELECT 1"})
        assert "DATABRICKS_ACCESS_TOKEN" in out
        # Round-2 review surfaced this: tell users that missing `sql` scope
        # is a likely cause on free-tier workspaces.
        assert "sql" in out.lower()

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


class TestQueryTimeout:
    """The tool layer must enforce a wall-clock query cap — `_socket_timeout`
    on the connector only caps individual HTTP requests, not total query
    duration. Without this, a runaway query blocks the MCP client forever."""

    async def test_long_query_returns_timeout_message(self, mcp_server, monkeypatch):
        from dbx.auth import AuthSource
        # Slash the timeout down to something quick so the test runs fast.
        monkeypatch.setattr("databricks_mcp._QUERY_TIMEOUT_S", 0.2)

        def hangs(query, *, token=None):
            time.sleep(5)  # well past the 0.2s cap
            return {"columns": ["x"], "rows": [[1]], "truncated": False}

        with patch("databricks_mcp.db.resolve_token_now",
                   return_value=("t", AuthSource.PAT)), \
             patch("databricks_mcp.db.run_query", side_effect=hangs):
            out = await _call(mcp_server, "run_query", {"query": "SELECT 1"})

        assert "exceeded" in out.lower()
        assert "0.2s" in out
        assert "LIMIT" in out


class TestEventLoopUnblocked:
    """Round-2 invariant: long Databricks queries must not block other
    concurrent MCP requests. Asserted via a threading.Barrier — if the
    runtime serializes tool calls, the second call never reaches the barrier
    and we deadlock (caught via wait timeout)."""

    async def test_concurrent_tool_calls_run_in_parallel(self, mcp_server):
        from dbx.auth import AuthSource

        # Both queries must reach the barrier before either can return.
        # If asyncio.to_thread isn't actually offloading, the second call
        # can't reach the barrier (first holds the event loop) → BrokenBarrier.
        barrier = threading.Barrier(2, timeout=3.0)

        def gated(query, *, token=None):
            barrier.wait()
            return {"columns": ["x"], "rows": [[1]], "truncated": False}

        with patch("databricks_mcp.db.resolve_token_now",
                   return_value=("t", AuthSource.PAT)), \
             patch("databricks_mcp.db.run_query", side_effect=gated):
            from fastmcp import Client
            async with Client(mcp_server) as client:
                await asyncio.gather(
                    client.call_tool("run_query", {"query": "q1"}),
                    client.call_tool("run_query", {"query": "q2"}),
                )

    async def test_db_run_query_runs_in_worker_thread(self, mcp_server):
        """Verify the offload actually happens — db.run_query must not run
        on the asyncio event-loop thread."""
        from dbx.auth import AuthSource
        seen_threads = []

        def record(query, *, token=None):
            seen_threads.append(threading.current_thread().ident)
            return {"columns": ["x"], "rows": [[1]], "truncated": False}

        with patch("databricks_mcp.db.resolve_token_now",
                   return_value=("t", AuthSource.PAT)), \
             patch("databricks_mcp.db.run_query", side_effect=record):
            await _call(mcp_server, "run_query", {"query": "q"})

        main_tid = threading.main_thread().ident
        assert seen_threads, "db.run_query was never invoked"
        assert seen_threads[0] != main_tid, (
            "db.run_query ran on the main thread — asyncio.to_thread offload "
            "is not in effect."
        )


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


class TestTokenThreading:
    async def test_resolved_token_is_passed_to_client(self, mcp_server, monkeypatch):
        from dbx.auth import AuthSource
        captured = {}

        def fake_run_query(query, *, token=None):
            captured["query"] = query
            captured["token"] = token
            return {"columns": ["x"], "rows": [[1]], "truncated": False}

        with patch("databricks_mcp.db.resolve_token_now",
                   return_value=("captured-bearer", AuthSource.BEARER)), \
             patch("databricks_mcp.db.run_query", side_effect=fake_run_query):
            await _call(mcp_server, "run_query", {"query": "SELECT x"})

        assert captured["token"] == "captured-bearer"

    async def test_token_threaded_to_list_helpers(self, mcp_server):
        from dbx.auth import AuthSource
        seen_tokens = []

        def grab_token(*args, **kwargs):
            seen_tokens.append(kwargs.get("token"))
            return []

        with patch("databricks_mcp.db.resolve_token_now",
                   return_value=("the-tok", AuthSource.OAUTH)), \
             patch("databricks_mcp.db.list_catalogs", side_effect=grab_token), \
             patch("databricks_mcp.db.list_schemas", side_effect=grab_token), \
             patch("databricks_mcp.db.list_tables", side_effect=grab_token):
            await _call(mcp_server, "list_catalogs")
            await _call(mcp_server, "list_schemas", {"catalog": "main"})
            await _call(mcp_server, "list_tables", {"catalog": "main", "schema": "default"})

        assert seen_tokens == ["the-tok", "the-tok", "the-tok"]


