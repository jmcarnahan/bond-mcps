# bond-mcps

Standalone home for the Bond MCP (Model Context Protocol) servers and their shared OAuth/auth library. Each MCP is an isolated Python package that can run locally as a CLI or as a `streamable-http` MCP server. All MCPs share the common `auth` package for OAuth callback relay and token storage.

## Layout

```
bond-mcps/
├── auth/                # Shared OAuth callback proxy + token store (Python package: `auth`)
├── mcps/
│   ├── microsoft/       # Microsoft Graph MCP (Mail, Teams, OneDrive, SharePoint) — port 5557
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
  CLI ──> auth proxy (port 8000) ──> browser ──> token cache (~/.bond_ai_tokens/)
```

## Quick start (local development)

**Prereqs:** Python ≥ 3.10, Poetry.

### 1. Install the auth library

```bash
cd auth
poetry install
```

### 2. Start the auth callback proxy (separate terminal — only needed for local OAuth flows)

```bash
cd auth
poetry run python -m auth          # defaults to port 8000
```

### 3. Install and run an MCP

```bash
cd mcps/microsoft
poetry install
poetry run pytest -q                                                            # tests
poetry run ms-graph-cli --help                                                  # CLI
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 5557  # server
```

Substitute `github` (CLI: `github-cli`, port 5558) or `atlassian` (CLI: `atlassian-cli`, port 9001) for the other MCPs.

## Configuration

Each MCP reads its OAuth client credentials and config from environment variables (see each MCP's README for specifics). For the calling-backend (Bearer-token) path, no client-side OAuth env vars are required — the backend handles the OAuth flow and passes a token via the `Authorization` header.

The `auth` proxy defaults to port 8000; override with `BOND_AUTH_PROXY_PORT`. Token cache lives in `~/.bond_ai_tokens/`.

## Deployment

A shared deployment target (ECS Express vs Fargate vs EKS) is being designed. Existing per-MCP `deployment/` directories (App Runner Terraform) are copied as legacy references and will be replaced.

## Adding a new MCP

1. Create `mcps/<name>/` with its own `pyproject.toml` declaring `bond-auth = {path = "../../auth", develop = true}`
2. Write the MCP server module using FastMCP (see `mcps/microsoft/ms_graph_mcp.py` as a template)
3. Add tests using `respx` for HTTP mocking
4. Register the CLI entry in `[tool.poetry.scripts]`
5. Update this README's table
