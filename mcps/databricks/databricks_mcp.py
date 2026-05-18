#!/usr/bin/env python3
"""
Databricks MCP Server.

Executes SQL against a Databricks SQL warehouse. Token resolution (see
dbx/auth.py):
  1. Authorization: Bearer header (backend mode, e.g. Bond AI forwarding a token)
  2. Local OAuth U2M when DATABRICKS_CLIENT_ID is set
  3. PAT (DATABRICKS_ACCESS_TOKEN) — dev fallback for workspaces that cannot
     register OAuth apps (free / Community Edition)

The token is resolved synchronously in each tool's call frame (so FastMCP's
request-scoped Bearer header is captured) and then handed to a worker thread
via asyncio.to_thread for the actual SQL round-trip — long queries no longer
block the event loop. Each tool wraps its asyncio.to_thread in
`asyncio.wait_for(timeout=_QUERY_TIMEOUT_S)` so a runaway query returns a
prompt error to the client. The background worker thread continues until
the query naturally finishes (Python has no clean way to cancel a thread);
its result is discarded.

Run (standalone):
    make dev                                                                    # all 4 services
    poetry run fastmcp run databricks_mcp.py --transport streamable-http --port 18004

Tool summary (4 tools):
  SQL  : run_query, list_catalogs, list_schemas, list_tables
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env")

from dbx import client as db
from dbx.auth import AuthSource
from dbx.client import DatabricksError, _MAX_FETCH_ROWS
from dbx.errors import friendly_error, format_table, stringify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MAX_PREVIEW_ROWS = 50

# Total wall-clock cap on a single tool invocation. If it expires, the tool
# returns a timeout message; the background thread is orphaned but bounded by
# the SQL connector's per-request _SOCKET_TIMEOUT_S (each polling call caps
# at 5 minutes, so an orphan thread eventually self-cleans).
_QUERY_TIMEOUT_S = 300


@asynccontextmanager
async def _lifespan(app):
    """Log the auth mode at startup. Validate the auth proxy only when OAuth
    is configured — PAT and backend modes do not need the proxy."""
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        from auth import OAuthProxyClient
        proxy = OAuthProxyClient()
        try:
            proxy.check_proxy()
            logger.info("Databricks auth: OAuth mode (proxy validated)")
        except RuntimeError as e:
            logger.warning("Databricks auth: OAuth mode (proxy NOT available): %s", e)
    elif os.environ.get("DATABRICKS_ACCESS_TOKEN"):
        logger.info("Databricks auth: PAT mode (DATABRICKS_ACCESS_TOKEN)")
    else:
        logger.info(
            "Databricks auth: backend mode (expecting Authorization: Bearer "
            "on incoming requests)"
        )
    yield


mcp = FastMCP("Databricks MCP Server", lifespan=_lifespan)


def _format_result(result: dict, query: str) -> str:
    """Render a query result dict as a string.

    Layout (always — no two-structure mode):
        N row(s)[+]:[ — showing first M]
        <pipe-delimited table of preview rows>
        [(Fetch was capped at _MAX_FETCH_ROWS rows ...)]

    The `+` suffix on the count indicates the fetch hit the cap and there
    may be more rows in the warehouse.
    """
    columns = result["columns"]
    rows = result["rows"]
    truncated = result.get("truncated", False)

    if not columns:
        return "Query executed (no result set returned)."

    total = len(rows)
    if total == 0:
        return f"No rows returned.\n\nColumns: {', '.join(columns)}"

    preview = rows[:_MAX_PREVIEW_ROWS]
    table = format_table(
        columns, [[stringify(v) for v in row] for row in preview]
    )

    suffix_count_marker = "+" if truncated else ""
    if total > _MAX_PREVIEW_ROWS:
        header = (
            f"{total}{suffix_count_marker} row(s) — showing first "
            f"{_MAX_PREVIEW_ROWS}:"
        )
    else:
        header = f"{total}{suffix_count_marker} row(s):"

    out = f"{header}\n{table}"
    if truncated:
        out += (
            f"\n\n(Fetch was capped at {_MAX_FETCH_ROWS} rows — refine your "
            f"query with LIMIT to see more.)"
        )
    return out


def _capture_token() -> tuple[str | None, AuthSource | None, str | None]:
    """Resolve token synchronously in the async tool context. Returns
    (token, source, error_message). On error: (None, None, message)."""
    try:
        token, source = db.resolve_token_now()
        return token, source, None
    except PermissionError as e:
        return None, None, str(e)


async def _execute_with_timeout(fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` in a worker thread bounded by
    `_QUERY_TIMEOUT_S` wall-clock seconds. Returns the result or raises
    asyncio.TimeoutError."""
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=_QUERY_TIMEOUT_S,
    )


def _timeout_message() -> str:
    return (
        f"Query exceeded the {_QUERY_TIMEOUT_S}s timeout. Refine with LIMIT "
        f"or simplify the query. (The warehouse-side query may continue until "
        f"it finishes naturally; its result is discarded.)"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def run_query(query: str) -> str:
    """Execute a SQL query against the configured Databricks SQL warehouse.

    Args:
        query: A single SQL statement. Multi-statement queries are not
               supported; use one statement per call.
    """
    if not query.strip():
        return "Parameter 'query' is required."
    token, source, err = _capture_token()
    if err:
        return err
    try:
        result = await _execute_with_timeout(db.run_query, query, token=token)
    except asyncio.TimeoutError:
        return _timeout_message()
    except DatabricksError as e:
        return friendly_error(e, source)
    except PermissionError as e:
        # Defensive: should be unreachable because _capture_token already
        # handles missing auth. Kept so a refactor that drops a token through
        # the thread layer can't leak a stack trace to the MCP client.
        return str(e)
    return _format_result(result, query)


@mcp.tool()
async def list_catalogs() -> str:
    """List all catalogs the current user can see (SHOW CATALOGS)."""
    token, source, err = _capture_token()
    if err:
        return err
    try:
        catalogs = await _execute_with_timeout(db.list_catalogs, token=token)
    except asyncio.TimeoutError:
        return _timeout_message()
    except DatabricksError as e:
        return friendly_error(e, source)
    except PermissionError as e:
        return str(e)
    if not catalogs:
        return "No catalogs visible to your user."
    return f"{len(catalogs)} catalog(s):\n" + "\n".join(f"  {c}" for c in catalogs)


@mcp.tool()
async def list_schemas(catalog: str) -> str:
    """List schemas (a.k.a. databases) in a catalog.

    Args:
        catalog: Catalog name (e.g., "main", "samples").
    """
    if not catalog:
        return "Parameter 'catalog' is required."
    token, source, err = _capture_token()
    if err:
        return err
    try:
        schemas = await _execute_with_timeout(
            db.list_schemas, catalog, token=token
        )
    except asyncio.TimeoutError:
        return _timeout_message()
    except DatabricksError as e:
        return friendly_error(e, source)
    except PermissionError as e:
        return str(e)
    if not schemas:
        return f"No schemas visible in catalog `{catalog}`."
    return f"{len(schemas)} schema(s) in `{catalog}`:\n" + "\n".join(
        f"  {s}" for s in schemas
    )


@mcp.tool()
async def list_tables(catalog: str, schema: str) -> str:
    """List tables in a catalog.schema.

    Args:
        catalog: Catalog name.
        schema:  Schema (database) name within the catalog.
    """
    if not catalog or not schema:
        return "Parameters 'catalog' and 'schema' are both required."
    token, source, err = _capture_token()
    if err:
        return err
    try:
        tables = await _execute_with_timeout(
            db.list_tables, catalog, schema, token=token
        )
    except asyncio.TimeoutError:
        return _timeout_message()
    except DatabricksError as e:
        return friendly_error(e, source)
    except PermissionError as e:
        return str(e)
    if not tables:
        return f"No tables visible in `{catalog}`.`{schema}`."
    rows = [
        [t.get("database", ""), t.get("table", ""), "yes" if t.get("is_temporary") else "no"]
        for t in tables
    ]
    return f"{len(tables)} table(s) in `{catalog}`.`{schema}`:\n" + format_table(
        ["database", "table", "temp"], rows
    )


if __name__ == "__main__":
    mcp.run()
