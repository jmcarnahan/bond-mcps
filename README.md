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
| Microsoft | [`mcps/microsoft/`](mcps/microsoft/) | 18001 | `ms-graph-cli` |
| GitHub | [`mcps/github/`](mcps/github/) | 18002 | `github-cli` |
| Atlassian | [`mcps/atlassian/`](mcps/atlassian/) | 18003 | `atlassian-cli` |

The MCP ports are configurable — see the `Makefile` (`MS_GRAPH_PORT`, `GITHUB_PORT`, `ATLASSIAN_PORT`). The auth proxy port (`8000`) is fixed by the OAuth callback URIs registered in each provider's OAuth app, so changing it requires updating those app registrations.

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
- The OAuth app's **redirect URI** must include `http://localhost:8000/connections/<provider>/callback` so the local auth proxy can receive callbacks. If you override `BOND_AUTH_PROXY_PORT`, you must update the registered redirect URI in each provider's OAuth app config — otherwise the callback will be rejected.

### Required environment variables per MCP

Create a `.env` in each MCP directory (or `export` the values). These are needed only for local OAuth/CLI use — when a backend like Bond AI is supplying Bearer tokens, none of these are required.

| MCP | Required | Optional |
|---|---|---|
| `mcps/microsoft/` | `MS_CLIENT_ID` | `MS_CLIENT_SECRET` (required if Azure app is a confidential client), `MS_TENANT_ID` (defaults to `consumers`), `MS_DEFAULT_FROM_ADDRESS` |
| `mcps/github/` | `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | — (the CLI and MCP server share this OAuth app; PKCE first, device-code fallback) |
| `mcps/atlassian/` | `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET` | `ATLASSIAN_ACCESS_TOKEN` + `ATLASSIAN_CLOUD_ID` (bypass OAuth flow entirely) |

`.env` files are gitignored. The auth proxy port can be overridden with `BOND_AUTH_PROXY_PORT`.

**Precedence**: shell-exported env vars win over values in `.env` (standard `python-dotenv` behavior). If something in `.env` "isn't working," check `env | grep <VAR>` first.

## Quick start (local development)

The repo-root `Makefile` orchestrates all four processes (auth proxy + 3 MCPs). The "by hand" path below is shown for debugging individual components.

### 0. Populate `.env` per MCP (one-time)

```bash
cp mcps/microsoft/.env.example mcps/microsoft/.env   # then fill in MS_CLIENT_ID etc.
cp mcps/github/.env.example    mcps/github/.env      # then fill in GITHUB_CLIENT_ID + SECRET
cp mcps/atlassian/.env.example mcps/atlassian/.env   # then fill in ATLASSIAN_CLIENT_ID + SECRET
```

Without these, the MCP servers boot but every tool call fails with `PermissionError`. See each per-MCP README for OAuth app registration steps.

### 1. Start everything

```bash
make install          # poetry install in auth/ + each MCP
make dev              # boots auth proxy + 3 MCPs in the background
make status           # shows [up]/[down] per service
make logs             # tail all log files (Ctrl-C to detach; processes keep running)
make stop             # shut everything down
```

`make dev` first runs `check-ports` (uses `lsof`) and refuses to start if any of 8000/18001/18002/18003 is already in use — it names the offending process. Override any port: `MS_GRAPH_PORT=29001 make dev`, or `AUTH_PORT=9000 make dev` for the auth proxy (the proxy reads `BOND_AUTH_PROXY_PORT`, which the Makefile sets from `AUTH_PORT`).

Logs land in `tmp/logs/` and are gitignored.

### By hand: a single MCP

```bash
# Terminal 1 — auth proxy (required for any local OAuth flow)
cd auth && poetry install && poetry run python -m auth      # 127.0.0.1:8000

# Terminal 2 — MCP server (substitute github / atlassian as needed)
cd mcps/microsoft && poetry install
poetry run pytest -q                                                              # tests
poetry run ms-graph-cli whoami                                                    # CLI smoke test
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 18001   # server
```

For GitHub: CLI `github-cli`, port `18002`. For Atlassian: CLI `atlassian-cli`, port `18003`.

The first CLI invocation opens your browser for OAuth (unless a cached token is still valid). See the per-MCP README for the full CLI surface.

## Authenticate (prime token caches)

After `make dev`, drive the initial OAuth flow per provider. Do this *before* registering with Claude Code so the first MCP tool call doesn't have to wait on an interactive browser flow:

```bash
make login              # runs Microsoft, then GitHub, then Atlassian sequentially
make login-microsoft    # individual provider
make logout             # clear all cached tokens
make logout-github      # clear one
```

Each `login-*` target runs the matching CLI's user-info command — that triggers a browser-based OAuth flow if there's no valid cached token, and returns silently if a token is already cached. Login is sequential by design so the browser only opens one tab at a time.

The first invocation per provider opens your browser; subsequent ones return immediately from cache.

## Connect to Claude Code

With `make dev` running and tokens primed via `make login`, register the MCPs with Claude Code at **user scope** so they're available in every project:

```bash
make claude-add                 # registers all three at user scope (idempotent)
claude mcp list                 # all three should show "connected"
```

Manual equivalent (default ports):

```bash
claude mcp add --transport http --scope user ms-graph  http://localhost:18001/mcp
claude mcp add --transport http --scope user github    http://localhost:18002/mcp
claude mcp add --transport http --scope user atlassian http://localhost:18003/mcp
```

The MCP servers must be running whenever Claude Code is. Use `make stop` to shut down between sessions, `make claude-remove` to unregister.

## Token database

OAuth tokens are stored in an encrypted SQLAlchemy database. By default this is a SQLite file at `<repo>/tokens.db` (gitignored); set `BOND_MCPS_DB_URL` to point at PostgreSQL / Aurora RDS for a deployed setup.

| Setting | Default | Notes |
|---|---|---|
| `BOND_MCPS_DB_URL` | `sqlite:///<repo>/tokens.db` | Postgres URLs must include `sslmode=require\|verify-ca\|verify-full` |
| `BOND_MCPS_ENCRYPTION_KEY` | (none) | base64-encoded 32-byte AES-256 key. Required for Postgres; for SQLite, falls back to a 0600 file at `~/.bond_mcps/encryption_key` (logs a WARN on every startup) |
| `BOND_MCPS_USER_ID` | `getpass.getuser()` | identifies who the tokens belong to. Required for Postgres deployments — refused at start if missing |

Tokens are encrypted with AES-256-GCM. Each ciphertext is bound to `(user_key, provider, field, key_version)` as AEAD associated data, so cross-row or cross-field tampering is rejected. Plaintext tokens never appear in the DB.

### One-time setup

```bash
make install              # installs deps + runs `bond-mcps migrate-db`
cd auth && poetry run bond-mcps generate-key   # mint a key
export BOND_MCPS_ENCRYPTION_KEY=<paste here>
cd auth && poetry run bond-mcps doctor          # validates URL, schema, encryption
```

### Day-to-day

| Action | Command |
|---|---|
| Re-auth a provider | `make logout-<provider>` (clears DB row) then `make login-<provider>` |
| Wipe everything | `rm -f tokens.db tokens.db-*` |
| Run schema migrations | `make migrate-db` |
| Health check | `make doctor` |

### Migrating from the legacy file cache

If you have existing tokens in `~/.bond_mcps/*.json` from a prior version: `make import-tokens` imports them into the encrypted DB and moves the original files to `~/.bond_mcps/legacy_imported/<provider>.json.<timestamp>` (preserved for recovery). Per-provider: GitHub/Atlassian go into the `provider_tokens` table; Microsoft's MSAL cache blob goes into `msal_token_caches`.

The older `make migrate-tokens` target still handles paths from the pre-`bond_mcps` era (`~/.ms_graph_tokens.json`, `~/.bond_ai_tokens/`) — run that first if you're coming from bond-ai, then `make import-tokens` to move into the DB.

## Known account-type limitations

Some tools are gated by the account/tenant you authenticated with, not by the MCP code:

- **Microsoft personal accounts** (`MS_TENANT_ID=consumers`, e.g. `*@outlook.com`, `*@hotmail.com`): Mail, Calendar, OneDrive (personal) work. **Teams, SharePoint, and Power BI require an organizational Microsoft 365 tenant** — use `MS_TENANT_ID=<your-tenant-guid>` and an Azure app registered in that tenant.
- **Atlassian Confluence v2 endpoints**: the code calls `/wiki/api/v2/spaces`, which requires the granular scope `read:space:confluence`. If your OAuth app was registered with only legacy scopes (`read:confluence-space.summary`), confluence calls return `401 scope does not match`. Add the granular scope to the OAuth app and re-authenticate. Jira is unaffected.
- **GitHub**: any repo or org accessible by the OAuth token works. No special gating.

## Configuration

OAuth client credentials and per-provider config are read from environment variables (see Prerequisites). For the calling-backend (Bearer-token) path, no client-side OAuth env vars are required — the backend handles the OAuth flow and passes a token via the `Authorization` header.

The `auth` proxy defaults to port 8000; override with `BOND_AUTH_PROXY_PORT`. Note that the OAuth app's registered redirect URI must match the port the proxy is actually listening on.

## Deployment

A shared deployment target (ECS Express vs Fargate vs EKS) is being designed and will live at the top-level `deployment/` directory. Each MCP also has a `deployment.legacy/` directory with App Runner Terraform copied verbatim from `bond-ai` — these still reference the old `../../shared_auth/` paths and will not apply as-is. They're kept as reference until the shared cluster work lands.

## Adding a new MCP

1. Create `mcps/<name>/` with its own `pyproject.toml` declaring `bond-auth = {path = "../../auth", develop = true}`
2. Write the MCP server module using FastMCP (see `mcps/microsoft/ms_graph_mcp.py` as a template)
3. Add tests using `respx` for HTTP mocking
4. Register the CLI entry in `[tool.poetry.scripts]`
5. Update this README's tables (MCP list + env vars + token caches)
