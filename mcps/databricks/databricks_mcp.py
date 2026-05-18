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
block the event loop.

Run (standalone):
    make dev                                                                    # all 4 services
    poetry run fastmcp run databricks_mcp.py --transport streamable-http --port 18004

Tool summary (4 tools):
  SQL  : run_query, list_catalogs, list_schemas, list_tables
"""

import asyncio
import csv
import io
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env")

from dbx import client as db
from dbx.auth import AuthSource
from dbx.client import DatabricksError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MAX_PREVIEW_ROWS = 50


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


def _format_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Pipe-delimited CSV, matching the atlassian MCP table formatter."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().rstrip("\r\n")


def _friendly_error(err: DatabricksError, source: AuthSource | None) -> str:
    """Map DatabricksError codes to user-readable messages.

    `source` is the AuthSource that produced the token for the failed call,
    captured at tool entry. Threading it through (rather than re-reading env
    vars at error-formatting time) ensures the message matches the actual
    auth path used.
    """
    code = err.error_code

    if code == "MissingConfig":
        return f"Databricks not configured: {err}"

    if code == "Unauthorized":
        if source is AuthSource.OAUTH:
            return (
                "Databricks authentication failed. Run `make login-databricks` "
                "to reconnect, or check that DATABRICKS_HOST matches the "
                "workspace where the OAuth app is registered."
            )
        if source is AuthSource.PAT:
            return (
                "Databricks PAT rejected. Your DATABRICKS_ACCESS_TOKEN may be "
                "expired, revoked, missing the `sql` scope, or scoped to a "
                "different workspace than DATABRICKS_HOST."
            )
        if source is AuthSource.BEARER:
            return (
                "Databricks authentication failed. The forwarded Bearer token "
                "may be expired or scoped to a different workspace."
            )
        return (
            "Databricks rejected the token. No auth source is currently "
            "configured — see README for setup."
        )

    if code == "Forbidden":
        return (
            "Databricks permission denied. Your user lacks access to the SQL "
            "warehouse at DATABRICKS_HTTP_PATH, or to the requested "
            "catalog/schema."
        )

    if code == "Unreachable":
        return (
            f"Cannot reach Databricks at DATABRICKS_HOST. Check the workspace "
            f"URL.\n({err})"
        )

    if code == "SQLError":
        return f"Databricks SQL error:\n```\n{err}\n```"

    return f"Databricks error: {err}"


def _format_result(result: dict, query: str) -> str:
    """Render a query result dict as a markdown-friendly string.

    Small results: pipe-delimited table inline.
    Larger results: first _MAX_PREVIEW_ROWS as pipe-delimited table + a note
    indicating truncation.
    Empty results: a friendly "no rows" message.
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
    table = _format_table(
        columns, [[_stringify(v) for v in row] for row in preview]
    )

    if total <= _MAX_PREVIEW_ROWS and not truncated:
        return f"{total} row(s):\n{table}"

    # Either there are more preview rows than we'll show, or the cursor was
    # cut off at the fetch cap (truncated=True).
    if truncated:
        suffix = (
            f"\n\n(showing first {min(total, _MAX_PREVIEW_ROWS)} of "
            f"{total}+ row(s); fetch was capped — refine your query with "
            f"LIMIT to see all rows.)"
        )
    else:
        suffix = (
            f"\n\n(showing first {_MAX_PREVIEW_ROWS} of {total} row(s); "
            f"refine with LIMIT to narrow the result set.)"
        )
    return f"{table}{suffix}"


def _stringify(val) -> str:
    """Convert a SQL value to a stable string for table output."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _capture_token() -> tuple[str | None, AuthSource | None, str | None]:
    """Resolve token synchronously in the async tool context. Returns
    (token, source, error_message). On error: (None, None, message)."""
    try:
        token, source = db.resolve_token_now()
        return token, source, None
    except PermissionError as e:
        return None, None, str(e)


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
        result = await asyncio.to_thread(db.run_query, query, token=token)
    except DatabricksError as e:
        return _friendly_error(e, source)
    return _format_result(result, query)


@mcp.tool()
async def list_catalogs() -> str:
    """List all catalogs the current user can see (SHOW CATALOGS)."""
    token, source, err = _capture_token()
    if err:
        return err
    try:
        catalogs = await asyncio.to_thread(db.list_catalogs, token=token)
    except DatabricksError as e:
        return _friendly_error(e, source)
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
        schemas = await asyncio.to_thread(db.list_schemas, catalog, token=token)
    except DatabricksError as e:
        return _friendly_error(e, source)
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
        tables = await asyncio.to_thread(
            db.list_tables, catalog, schema, token=token
        )
    except DatabricksError as e:
        return _friendly_error(e, source)
    if not tables:
        return f"No tables visible in `{catalog}`.`{schema}`."
    rows = [
        [t.get("database", ""), t.get("table", ""), "yes" if t.get("is_temporary") else "no"]
        for t in tables
    ]
    return f"{len(tables)} table(s) in `{catalog}`.`{schema}`:\n" + _format_table(
        ["database", "table", "temp"], rows
    )


if __name__ == "__main__":
    mcp.run()
