# bond-mcps

Standalone home for the Bond MCP (Model Context Protocol) servers and their shared OAuth/auth library. Each MCP is an isolated Python package that can run locally as a CLI or as a `streamable-http` MCP server. All MCPs share the common `auth` package for OAuth callback relay and token storage.

## Layout

```
bond-mcps/
├── auth/                # Shared OAuth callback proxy + token store (Python package: `auth`)
├── mcps/
│   ├── microsoft/       # Microsoft Graph MCP (Mail, Calendar, Teams, OneDrive, SharePoint) — port 5557
│   ├── github/          # GitHub MCP (repos, issues, PRs, code) — port 5558
│   └── atlassian/       # Atlassian MCP (Jira, Confluence) — port 9001
└── deployment/          # Shared cluster infra (planned)
```

| MCP | Directory | Port | CLI |
|-----|-----------|------|-----|
| Microsoft | [`mcps/microsoft/`](mcps/microsoft/) | 5557 | `ms-graph-cli` |
| GitHub | [`mcps/github/`](mcps/github/) | 5558 | `github-cli` |
| Atlassian | [`mcps/atlassian/`](mcps/atlassian/) | 9001 | `atlassian-cli` |

## Architecture

Each MCP:
- Receives pre-authenticated Bearer tokens from a calling backend (e.g. Bond AI)
- Exposes tools via the MCP protocol (FastMCP, `streamable-http` transport)
- Runs as a standalone process — locally for development, or in a shared cloud cluster (deployment TBD)
- For local-only use, can drive its own OAuth flow via the `auth` package's callback proxy

```
Client (Bond AI backend, Claude Code, MCP client)
    │  Authorization: Bearer <user-token>
    ▼
MCP server (FastMCP, streamable-http)
    │
    └──> External provider API (Microsoft Graph / GitHub / Atlassian)

For local OAuth (no backend):
  CLI ──> auth proxy (port 8000) ──> browser ──> provider login ──> token cache
```

## Prerequisites

- **Python ≥ 3.10**, **Poetry**
- An **OAuth app registration** with the provider you want to use (see the per-MCP README for setup steps):
  - Microsoft → Azure App Registration ([`mcps/microsoft/README.md`](mcps/microsoft/README.md))
  - GitHub → GitHub OAuth App ([`mcps/github/README.md`](mcps/github/README.md))
  - Atlassian → Atlassian OAuth 2.0 integration ([`mcps/atlassian/README.md`](mcps/atlassian/README.md))
- The OAuth app's **redirect URI** must include `http://localhost:8000/connections/<provider>/callback` so the local auth proxy can receive callbacks.

### Required environment variables per MCP

Create a `.env` in each MCP directory (or `export` the values). These are needed only for local OAuth/CLI use — when a backend like Bond AI is supplying Bearer tokens, none of these are required.

| MCP | Required | Optional |
|---|---|---|
| `mcps/microsoft/` | `MS_CLIENT_ID` | `MS_CLIENT_SECRET` (required if Azure app is a confidential client), `MS_TENANT_ID` (defaults to `consumers`), `MS_DEFAULT_FROM_ADDRESS` |
| `mcps/github/` | `GH_CLIENT_ID` (device-code flow used by the CLI) | `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` (used by the MCP server's proxy/PKCE flow) |
| `mcps/atlassian/` | `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET` | `ATLASSIAN_ACCESS_TOKEN` + `ATLASSIAN_CLOUD_ID` (bypass OAuth flow entirely) |

`.env` files are gitignored. The auth proxy port can be overridden with `BOND_AUTH_PROXY_PORT`.

## Quick start (local development)

### 1. Install the auth library

```bash
cd auth
poetry install
```

### 2. Start the auth callback proxy

Leave this running in a dedicated terminal. The MCP server / CLI checks it at startup and exits if it's not reachable.

```bash
cd auth
poetry run python -m auth          # defaults to 127.0.0.1:8000
```

You should see `Bond AI OAuth Proxy — Listening on 127.0.0.1:8000`.

### 3. Install and run an MCP

In a second terminal, set up the env vars from the table above, then:

```bash
cd mcps/microsoft
poetry install
poetry run pytest -q                                                            # tests
poetry run ms-graph-cli whoami                                                  # CLI smoke test
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 5557  # server
```

Substitute `github` (CLI: `github-cli`, port 5558) or `atlassian` (CLI: `atlassian-cli`, port 9001).

The first CLI invocation opens your browser for OAuth (unless a cached token is still valid). See the per-MCP README for the full CLI surface.

## Token caches

| MCP | Cache file | Notes |
|---|---|---|
| Microsoft | `~/.ms_graph_tokens.json` | MSAL-managed (separate from the shared TokenStore) |
| GitHub | `~/.bond_ai_tokens/github.json` | Long-lived OAuth token, no auto-refresh |
| Atlassian | `~/.bond_ai_tokens/atlassian.json` | Auto-refresh via `refresh_token` if `ATLASSIAN_CLIENT_ID/SECRET` are set |

To force re-auth for one provider, delete its cache file.

## Known account-type limitations

Some tools are gated by the account/tenant you authenticated with, not by the MCP code:

- **Microsoft personal accounts** (`MS_TENANT_ID=consumers`, e.g. `*@outlook.com`, `*@hotmail.com`): Mail, Calendar, OneDrive (personal) work. **Teams, SharePoint, and Power BI require an organizational Microsoft 365 tenant** — use `MS_TENANT_ID=<your-tenant-guid>` and an Azure app registered in that tenant.
- **Atlassian Confluence v2 endpoints**: the code calls `/wiki/api/v2/spaces`, which requires the granular scope `read:space:confluence`. If your OAuth app was registered with only legacy scopes (`read:confluence-space.summary`), confluence calls return `401 scope does not match`. Add the granular scope to the OAuth app and re-authenticate. Jira is unaffected.
- **GitHub**: any repo or org accessible by the OAuth token works. No special gating.

## Configuration

OAuth client credentials and per-provider config are read from environment variables (see Prerequisites). For the calling-backend (Bearer-token) path, no client-side OAuth env vars are required — the backend handles the OAuth flow and passes a token via the `Authorization` header.

The `auth` proxy defaults to port 8000; override with `BOND_AUTH_PROXY_PORT`. Note that the OAuth app's registered redirect URI must match the port the proxy is actually listening on.

## Deployment

A shared deployment target (ECS Express vs Fargate vs EKS) is being designed. Existing per-MCP `deployment/` directories (App Runner Terraform copied from `bond-ai`) are kept as legacy references and will be replaced.

## Adding a new MCP

1. Create `mcps/<name>/` with its own `pyproject.toml` declaring `bond-auth = {path = "../../auth", develop = true}`
2. Write the MCP server module using FastMCP (see `mcps/microsoft/ms_graph_mcp.py` as a template)
3. Add tests using `respx` for HTTP mocking
4. Register the CLI entry in `[tool.poetry.scripts]`
5. Update this README's tables (MCP list + env vars + token caches)
