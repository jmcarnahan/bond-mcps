# Databricks MCP

SQL warehouse queries against a Databricks workspace, with OAuth U2M (recommended) or a PAT fallback for free-tier workspaces.

## Tools (4)

| Tool | Purpose |
|---|---|
| `run_query` | Execute a single SQL statement; returns a markdown table for ≤ 50 rows, CSV preview + summary for larger results. |
| `list_catalogs` | `SHOW CATALOGS` |
| `list_schemas` | `SHOW SCHEMAS IN <catalog>` |
| `list_tables` | `SHOW TABLES IN <catalog>.<schema>` |

## Quick start

```bash
cd mcps/databricks
poetry install
cp .env.example .env       # fill in DATABRICKS_HOST + HTTP_PATH and either OAuth or PAT
poetry run pytest -q       # 71 tests
poetry run databricks-cli whoami
poetry run fastmcp run databricks_mcp.py --transport streamable-http --port 18004
```

From the repo root, `make dev` boots this alongside the other MCPs and `make login-databricks` triggers OAuth (or short-circuits in PAT mode).

## Auth modes

The MCP resolves auth in this order, and OAuth always wins over PAT when both are configured:

1. **`Authorization: Bearer` header** — when running behind Bond AI (or any backend that forwards a Databricks OAuth token), no client config is needed.
2. **OAuth U2M** — set `DATABRICKS_CLIENT_ID` (and `DATABRICKS_CLIENT_SECRET` if your OAuth app is confidential). Browser PKCE flow via the shared auth proxy on port 8000. Token + refresh_token cached at `~/.bond_mcps/databricks.json` (mode 0600). Refresh is transparent — the SQL connector calls our `credentials_provider` on every HTTP request, and we re-resolve the token (refreshed via `TokenStore.refresh_if_needed` when expired).
3. **PAT** — set `DATABRICKS_ACCESS_TOKEN` to a personal access token. Intended for **free / Community Edition workspaces** that can't register OAuth apps. PAT is sent as the Bearer token directly; no caching, no refresh.

If nothing is configured, the MCP boots cleanly (so it can serve backend-mode requests) but every tool call fails with a friendly `PermissionError` describing all three setup paths.

## Connection env vars

| Var | Required | Notes |
|---|---|---|
| `DATABRICKS_HOST` | always | Workspace URL, e.g. `https://dbc-12345-abcd.cloud.databricks.com`. Scheme + trailing slash are normalized. |
| `DATABRICKS_HTTP_PATH` | always | SQL warehouse path, e.g. `/sql/1.0/warehouses/abcdef1234567890`. Get this from the warehouse's "Connection details" tab. |
| `DATABRICKS_CLIENT_ID` | OAuth mode | Workspace-level OAuth app client ID. |
| `DATABRICKS_CLIENT_SECRET` | OAuth (confidential apps) | Omit entirely for public PKCE-only apps — don't set an empty string. |
| `DATABRICKS_ACCESS_TOKEN` | PAT mode | A workspace PAT (string starting with `dapi…`). Stored in `.env` in plaintext — keep `.env` out of git (it's already gitignored). |

## OAuth app setup (workspace-level)

In the Databricks workspace admin console:

1. **Settings → Developer → App connections** → **Add OAuth client**.
2. **Redirect URI**: `http://localhost:8000/connections/databricks/callback` (must match exactly; change the port only if you also change `BOND_AUTH_PROXY_PORT` everywhere).
3. **Scopes**: `sql`, `offline_access`. The `sql` scope grants SQL warehouse access (least-privilege for this MCP). `offline_access` is required for refresh tokens — without it, you re-authenticate every hour.
4. Choose confidential (with secret) or public (PKCE only). For Bond AI dev use, confidential is the simpler default.
5. Copy the **client ID** (and **client secret** for confidential apps) into `mcps/databricks/.env`.

If your workspace's app-connections menu is greyed out (free / Community Edition), use the PAT path instead — see below.

## PAT setup (dev fallback)

1. In Databricks: **User Settings → Developer → Access tokens → Generate new token**.
2. Set `DATABRICKS_ACCESS_TOKEN=dapi…` in `mcps/databricks/.env` (alongside `DATABRICKS_HOST` and `DATABRICKS_HTTP_PATH`).
3. Leave `DATABRICKS_CLIENT_ID` unset, or the MCP will prefer OAuth.
4. `make login-databricks` (or `poetry run databricks-cli whoami` directly) will detect PAT mode, skip the browser, and run a `SELECT current_user()` to verify.

PAT scope is whatever the issuing user can see — there's no scope reduction. Don't use PATs in shared / production contexts.

## CLI

```bash
poetry run databricks-cli whoami                  # auth check + SELECT current_user()
poetry run databricks-cli query "SELECT 1"        # run any SQL
poetry run databricks-cli catalogs                # SHOW CATALOGS
poetry run databricks-cli schemas main            # SHOW SCHEMAS IN `main`
poetry run databricks-cli tables main default     # SHOW TABLES IN `main`.`default`
poetry run databricks-cli logout                  # clear cached OAuth token
```

## Why the Python package is named `dbx`

The `databricks-sql-connector` package installs into the `databricks.*` namespace (`databricks.sql`, `databricks.sqlalchemy`). A local Python package named `databricks` would shadow it. We use `dbx` for our package — matching the precedent set by `mcps/microsoft/`, whose package is `ms_graph` rather than `microsoft`. Entry-file names (`databricks_mcp.py`, `databricks_cli.py`) and the directory (`mcps/databricks/`) follow the provider-name convention as usual.

## Architecture notes

- **One connection per query.** MCP usage is interactive and low-QPS; pooling would re-introduce the token-expiry-vs-pool race that PAT-only code paths never had to worry about. The SQL connector handles its own per-call setup, and our `credentials_provider` callback supplies fresh tokens.
- **`credentials_provider` is called per HTTP request**, not once per connection. That's why OAuth refresh works transparently — the connector picks up the new token on the next round-trip.
- **Lazy auth**: the MCP server starts cleanly with no Databricks env vars (so backend mode works). Auth and connection checks happen on the first tool call, not at import time.
- **`MissingConfig` errors are raised at query time** with a clear list of missing variables, not at startup.
