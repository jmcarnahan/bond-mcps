# Atlassian MCP Server

MCP server providing Jira and Confluence tools. Runs standalone (Claude Code, CLI) or behind the Bond AI backend — token resolution at `atlassian/auth.py` tries Bearer header first, falls back to local OAuth when `ATLASSIAN_CLIENT_ID` is set.

## Quick Start

```bash
# Install dependencies
cd mcps/atlassian
poetry install

# Run tests
poetry run pytest tests/ -v

# Start MCP server locally
fastmcp run atlassian_mcp.py --transport streamable-http --port 18003
```

## MCP Tools (5 dispatcher tools)

The MCP server exposes **5 tools**, each a dispatcher that selects an operation via a `target` parameter. (The CLI flattens these into the friendly subcommands shown above — but external MCP clients like Claude Code or Bond AI call these 5 tools directly.)

### `jira_search`
Search and list Jira resources. `target` ∈ `{projects, issues, issue_count, versions, users, myself}`.

| `target` | Purpose | Required params |
|---|---|---|
| `projects` | List accessible Jira projects | — |
| `issues` | Search issues using JQL | `jql` |
| `issue_count` | Count issues matching JQL | `jql` |
| `versions` | List release versions for a project | `project_key` |
| `users` | Search users by name/email | `query` |
| `myself` | Current user's account ID, email, display name | — |

### `jira_get`
Get detailed Jira info for one issue. `target` ∈ `{issue, transitions}`.

| `target` | Purpose | Required params |
|---|---|---|
| `issue` | Full issue + comments | `issue_key` |
| `transitions` | List available workflow transitions for the issue | `issue_key` |

### `jira_manage`
Create, update, or transition Jira resources. `target` ∈ `{create_issue, update_issue, comment, transition, create_version}`.

| `target` | Purpose | Required params |
|---|---|---|
| `create_issue` | Create a new issue | `project_key`, `summary` |
| `update_issue` | Update issue fields | `issue_key` + ≥1 field |
| `comment` | Add comment to issue (supports `@{accountId}` mentions) | `issue_key`, `body` |
| `transition` | Move issue to another workflow state | `issue_key`, `transition_name` |
| `create_version` | Create a release version | `project_key`, `name` |

### `confluence_search`
Search and read Confluence content. `target` ∈ `{spaces, pages, page}`.

| `target` | Purpose | Required params |
|---|---|---|
| `spaces` | List accessible spaces | — |
| `pages` | Search pages/blogs using CQL | `query` |
| `page` | Get full page content + version | `page_id` |

### `confluence_manage`
Create or update Confluence pages. `target` ∈ `{create_page, update_page}`.

| `target` | Purpose | Required params |
|---|---|---|
| `create_page` | Create a new page in a space | `space_id`, `title`, `body` |
| `update_page` | Update an existing page | `page_id`, `title`, `body` (auto-detects version) |

## Atlassian OAuth App Setup

### 1. Create OAuth 2.0 App

1. Go to [developer.atlassian.com/console/myapps](https://developer.atlassian.com/console/myapps)
2. Create a new OAuth 2.0 integration
3. Add scopes:
   - `read:jira-work` — Read Jira issues and projects
   - `write:jira-work` — Create/update Jira issues
   - `read:confluence-content.all` — Read Confluence pages
   - `write:confluence-content` — Create/update Confluence pages
   - `read:me` — Read user profile
4. Set callback URL to `http://localhost:8000/connections/atlassian/callback` (for standalone use via the shared auth proxy) or your Bond AI backend OAuth callback (for backend mode)
5. Note your Client ID and Client Secret

### 2. Find Your Cloud ID

```bash
# Replace YOUR-DOMAIN with your Atlassian domain
curl -s https://YOUR-DOMAIN.atlassian.net/_edge/tenant_info | jq .cloudId
```

## CLI Usage

The CLI supports two authentication modes:

1. **Browser OAuth via the shared auth proxy (recommended)** — set `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET`; the CLI runs the OAuth flow on first use, caches the token at `~/.bond_mcps/atlassian.json` (shared with the MCP server), and auto-refreshes it via `refresh_token`. Requires the auth proxy to be running (`make dev` or `cd auth && poetry run python -m auth`).
2. **Direct token (CI / scripts)** — set `ATLASSIAN_ACCESS_TOKEN` (and `ATLASSIAN_CLOUD_ID`) to bypass OAuth entirely. No proxy needed, but no refresh — you renew the token yourself.

```bash
# Mode 1 (OAuth flow)
export ATLASSIAN_CLIENT_ID=<your-oauth-app-client-id>
export ATLASSIAN_CLIENT_SECRET=<your-oauth-app-client-secret>

# Mode 2 (direct token)
export ATLASSIAN_ACCESS_TOKEN=<your-token>
export ATLASSIAN_CLOUD_ID=<your-cloud-id>
```

```bash
# Jira — read
atlassian-cli jira projects
atlassian-cli jira search "project = PROJ AND status = Open" --max-results 10
atlassian-cli jira count "project = PROJ AND type = Bug"
atlassian-cli jira get PROJ-123
atlassian-cli jira transitions PROJ-123                              # List available transitions
atlassian-cli jira versions PROJ                                     # List project releases
atlassian-cli jira lookup-user "Jane Doe"                            # Find user by name/email

# Jira — write
atlassian-cli jira create PROJ "Fix login bug" --type Bug --priority High
atlassian-cli jira update PROJ-123 --summary "New title"
atlassian-cli jira comment PROJ-123 "Working on this"
atlassian-cli jira transition PROJ-123 "In Progress"
atlassian-cli jira create-version PROJ "1.2.0" --description "Q3 release"

# Confluence
atlassian-cli confluence spaces
atlassian-cli confluence search 'type = page AND text ~ "release notes"' --max-results 10
atlassian-cli confluence get <page_id>

# User
atlassian-cli user me

# Raw — direct MCP tool interface (debugging / testing)
# All raw commands require --target (matches the dispatcher tool's `target` parameter)
atlassian-cli raw jira-search --target issues --jql "project = PROJ" --max-results 5
atlassian-cli raw jira-search --target projects
atlassian-cli raw jira-get --target transitions --issue-key PROJ-123
atlassian-cli raw confluence-search --target spaces --max-results 5
atlassian-cli raw confluence-search --target pages --query 'text ~ "release"'

# Auth
atlassian-cli logout                                                 # Clear cached token
```

> **Confluence scope gotcha**: the code calls `/wiki/api/v2/spaces`, which requires the **granular** scope `read:space:confluence`. If your OAuth app was registered with only the legacy `read:confluence-space.summary` scope, confluence calls return `401 scope does not match`. Add the granular scope to your OAuth app and re-authenticate. Jira is unaffected.

## Standalone Use with Claude Code

The MCP server runs standalone with local OAuth — no Bond AI backend required. Browser-based authorization code + PKCE flow via the shared OAuth proxy. Auto-discovers `cloud_id` from the accessible-resources API and stores it alongside the token.

### Prerequisites

1. An Atlassian OAuth 2.0 app (see [Atlassian OAuth App Setup](#atlassian-oauth-app-setup) above)
2. **Callback URL** registered on the OAuth app: `http://localhost:8000/connections/atlassian/callback`
3. `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET` set in `mcps/atlassian/.env` (or your shell). `ATLASSIAN_CLOUD_ID` is optional — pin it only if you have multiple sites and want a specific one.

### Recommended: orchestrate via the repo-root Makefile

```bash
make install            # one-time
make dev                # auth proxy on :8000 + Atlassian MCP on :18003 (and the other two)
make claude-add         # registers ms-graph / github / atlassian with Claude Code at user scope
make login-atlassian    # opens browser for first-time auth (or returns cached info)
```

`claude mcp list` should show `atlassian` as ✓ Connected. Try in Claude Code:

> "What's my Atlassian profile?" or "List my Jira projects"

### By hand

```bash
# Terminal 1 — auth proxy
cd auth && poetry run python -m auth

# Terminal 2 — MCP server
cd mcps/atlassian
poetry install
poetry run fastmcp run atlassian_mcp.py --transport streamable-http --port 18003

# Register
claude mcp add --transport http --scope user atlassian http://localhost:18003/mcp
```

### Authenticate / re-authenticate

Token is cached at `~/.bond_mcps/atlassian.json` (shared with the CLI) and includes the discovered `cloud_id` + refresh token for silent renewal.

```bash
make logout-atlassian      # or: rm ~/.bond_mcps/atlassian.json
make login-atlassian       # browser opens for re-auth
```

## Bond AI Integration

Add to `BOND_MCP_CONFIG` in `.env`:

```json
{
  "mcpServers": {
    "atlassian_v2": {
      "url": "https://YOUR-MCP-URL.awsapprunner.com/mcp",
      "transport": "streamable-http",
      "auth_type": "oauth2",
      "oauth2_provider": "atlassian",
      "cloud_id": "YOUR-CLOUD-ID"
    }
  }
}
```

The Bond AI backend will:
1. Pass the user's OAuth token as `Authorization: Bearer {token}`
2. Pass the cloud ID as `X-Atlassian-Cloud-Id: {cloud_id}`

## Architecture

```
User Browser
    → Bond AI Frontend
    → Bond AI Backend (OAuth + Token Storage)
    → Atlassian MCP Server (this project)
    → Atlassian REST APIs (api.atlassian.com)
```

The MCP server is stateless — it receives the OAuth token and cloud ID via HTTP headers on every request.

## API Details

### Dual Base URLs

Unlike GitHub (1 base URL), Atlassian requires two:
- **Jira**: `https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3`
- **Confluence**: `https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2`

### Jira Search: New Endpoints

Uses the new (non-deprecated) endpoints:
- `GET /rest/api/3/search/jql` — token-based pagination
- `POST /rest/api/3/search/approximate-count` — efficient counting

### ADF (Atlassian Document Format)

Jira v3 requires descriptions and comments in ADF. Plain text is auto-wrapped:
```json
{"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "..."}]}]}
```

## Deployment (AWS)

> ⚠️ **Legacy — inherited from `bond-ai`, not yet adapted to bond-mcps.**
> The Terraform in `mcps/atlassian/deployment.legacy/` still references the old
> `../../shared_auth/` paths and will fail `terraform apply` as-is. A shared
> deployment target (ECS Express / Fargate) is being designed at the top-level
> `deployment/` directory and will replace these per-MCP modules. Treat the
> instructions below as reference only.

Create a tfvars file (e.g., `mcps/atlassian/deployment.legacy/atlassian-mcp.tfvars`):
```hcl
aws_region                 = "us-west-2"
environment                = "dev"
project_name               = "bond-ai"
existing_vpc_id            = "vpc-XXXXXXXXX"
mcp_atlassian_is_private   = true   # Set to false for public access
```

Deploy:
```bash
cd mcps/atlassian/deployment.legacy
terraform init
terraform apply -var-file=atlassian-mcp.tfvars
```

Creates:
- ECR repository for Docker image
- App Runner service with VPC egress
- VPC ingress connection (if `mcp_atlassian_is_private = true`)
- IAM roles for ECR access and CloudWatch logs
- Auto-scaling (min 1, max 2 instances)

**Private deployment** requires the main Bond AI deployment to have `has_private_mcp_services = true` (or `backend_is_private`/`frontend_is_private` set to `true`), which creates the shared `apprunner.requests` VPC endpoint.

> **Note**: If the main deployment's VPC endpoint is ever destroyed and recreated (e.g., toggling all private flags off then back on), you must re-apply this MCP deployment to update the ingress connection with the new endpoint ID.

## Troubleshooting

### "Authorization required" error
The MCP server resolved no token. **Standalone mode**: check that `ATLASSIAN_CLIENT_ID` is set, the auth proxy is running (`make status`), and `~/.bond_mcps/atlassian.json` exists (run `make login-atlassian` to populate it). **Backend mode**: confirm Bond AI is forwarding the `Authorization: Bearer` header.

### "Cloud ID required" error
The MCP server resolved a token but no cloud_id. **Standalone mode**: re-run `make login-atlassian` (the OAuth flow auto-discovers cloud_id and writes it to the cache) or pin one via `ATLASSIAN_CLOUD_ID`. **Backend mode**: ensure `cloud_id` is set in the MCP server config so the backend sends `X-Atlassian-Cloud-Id`.

### "Rate limited by Atlassian"
Atlassian enforces rate limits. The error message includes the retry-after time. Wait and try again.

### Transition fails with "not available"
The error will list available transitions. Use one of those names (case-insensitive).

### JQL syntax errors
The error message from Jira will include the specific JQL parsing error. Check [Jira JQL documentation](https://support.atlassian.com/jira-service-management-cloud/docs/use-advanced-search-with-jira-query-language-jql/).
