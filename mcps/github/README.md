# GitHub MCP Server for Bond AI

FastMCP server providing GitHub repository, issue, pull request, and code tools via the GitHub REST API.

## Tools (17)

### Repositories (3)
- `list_repositories` — List the authenticated user's repos
- `get_repository` — Get detailed info about a repo
- `search_repositories` — Search repos across GitHub

### Issues (5)
- `list_issues` — List issues in a repo (filterable by state, labels)
- `get_issue` — Get issue details with comments
- `create_issue` — Create a new issue
- `update_issue` — Update title, body, state, labels
- `add_issue_comment` — Comment on an issue

### Pull Requests (5)
- `list_pull_requests` — List PRs (filter by state)
- `get_pull_request` — Get PR details with diff stats
- `create_pull_request` — Create a PR
- `add_pr_comment` — Comment on a PR
- `merge_pull_request` — Merge a PR (merge/squash/rebase)

### Code & Content (3)
- `get_file_content` — Read a file from a repo
- `create_or_update_file` — Create or update a file (commit directly)
- `search_code` — Search code across repos

### User (1)
- `get_authenticated_user` — Get current user info

## Quick Start

### Install Dependencies
```bash
cd mcps/github
poetry install
```

### Run Locally
```bash
poetry run fastmcp run github_mcp.py --transport streamable-http --port 18002
```

### Run Tests
```bash
poetry run pytest tests/ -v
```

### CLI

The CLI shares the same OAuth machinery as the MCP server: cached token first, then browser PKCE via the shared auth proxy (`make dev` or `cd auth && poetry run python -m auth`), then device-code fallback if the browser path fails.

```bash
# Set in mcps/github/.env or export in your shell:
export GITHUB_CLIENT_ID=<your-oauth-app-client-id>
export GITHUB_CLIENT_SECRET=<your-oauth-app-client-secret>

# Repos
poetry run github-cli repos list                                     # Your repositories
poetry run github-cli repos list --type owner --sort updated
poetry run github-cli repos get <owner> <repo>
poetry run github-cli repos search "fastmcp language:python"

# Issues
poetry run github-cli issues list <owner> <repo> --state open
poetry run github-cli issues get <owner> <repo> <issue_number>
poetry run github-cli issues create <owner> <repo> "Title" --body "Body"

# Pull requests
poetry run github-cli pulls list <owner> <repo> --state open
poetry run github-cli pulls get <owner> <repo> <pr_number>

# Code
poetry run github-cli code search "FastMCP repo:owner/repo"
poetry run github-cli code get <owner> <repo> <path> [--ref <branch>]

# User
poetry run github-cli user
```

The CLI and MCP server use the **same** cached token at `~/.bond_mcps/github.json`. Run either once and the other one inherits the auth. To force re-auth:

```bash
make logout-github      # or: rm ~/.bond_mcps/github.json
```

## Standalone Use with Claude Code

The MCP server runs standalone with local OAuth — no Bond AI backend required. Browser-based authorization code + PKCE flow via the shared OAuth proxy, with device code fallback for headless environments.

### Prerequisites

1. A GitHub OAuth App (see [GitHub OAuth App Setup](#github-oauth-app-setup) below)
2. **Callback URL** registered on the OAuth app: `http://localhost:8000/connections/github/callback`
3. `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` set in `mcps/github/.env` (or your shell)

### Recommended: orchestrate via the repo-root Makefile

```bash
make install            # one-time
make dev                # auth proxy on :8000 + GitHub MCP on :18002 (and the other two)
make claude-add         # registers ms-graph / github / atlassian with Claude Code at user scope
make login-github       # opens browser for first-time auth (or returns cached info)
```

`claude mcp list` should show `github` as ✓ Connected. Try in Claude Code:

> "What's my GitHub profile?" or "List my repositories"

### By hand

```bash
# Terminal 1 — auth proxy
cd auth && poetry run python -m auth

# Terminal 2 — MCP server
cd mcps/github
poetry install
poetry run fastmcp run github_mcp.py --transport streamable-http --port 18002

# Register
claude mcp add --transport http --scope user github http://localhost:18002/mcp
```

### Authenticate / re-authenticate

Token is cached at `~/.bond_mcps/github.json` and shared with the CLI. GitHub OAuth tokens don't expire by default — re-auth is only needed if you revoke the token.

```bash
make logout-github      # or: rm ~/.bond_mcps/github.json
make login-github       # browser opens for re-auth
```

## Bond AI Integration

### BOND_MCP_CONFIG (Local Development)
```json
{
  "mcpServers": {
    "github": {
      "url": "http://localhost:18002/mcp",
      "auth_type": "oauth2",
      "transport": "streamable-http",
      "display_name": "GitHub",
      "description": "Repositories, issues, pull requests, and code",
      "oauth_config": {
        "provider": "github",
        "client_id": "<CLIENT_ID>",
        "client_secret": "<CLIENT_SECRET>",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "scopes": "repo user read:org",
        "redirect_uri": "http://localhost:8000/connections/github/callback"
      }
    }
  }
}
```

### Production
Use `client_secret_arn` instead of `client_secret` to reference AWS Secrets Manager.

## Authentication

The MCP server resolves a token in this order (see `github/auth.py`):

1. **`Authorization: Bearer` header** — backend mode. Bond AI (or any other backend) handles the OAuth dance and forwards the access token on each request.
2. **Local OAuth** via `github/local_auth.py` — standalone mode. Activated when `GITHUB_CLIENT_ID` is set. Browser PKCE through the shared auth proxy on :8000, device-code fallback, cached at `~/.bond_mcps/github.json`. Same code path as the CLI.

GitHub OAuth tokens are long-lived (no refresh token, no expiry). Once authorized, the token works until the user revokes it on GitHub.

## GitHub OAuth App Setup

1. Go to https://github.com/settings/developers → OAuth Apps → New OAuth App
2. Set callback URL to your backend's `/connections/github/callback`
3. Copy Client ID and generate a Client Secret
4. Store the secret in AWS Secrets Manager for production

## Deployment (AWS)

> ⚠️ **Legacy — inherited from `bond-ai`, not yet adapted to bond-mcps.**
> The Terraform in `mcps/github/deployment.legacy/` still references the old
> `../../shared_auth/` paths and will fail `terraform apply` as-is. A shared
> deployment target (ECS Express / Fargate) is being designed at the top-level
> `deployment/` directory and will replace these per-MCP modules. Treat the
> instructions below as reference only.

Create a tfvars file (e.g., `mcps/github/deployment.legacy/github-mcp.tfvars`):
```hcl
aws_region              = "us-west-2"
environment             = "dev"
project_name            = "bond-ai"
existing_vpc_id         = "vpc-XXXXXXXXX"
mcp_github_is_private   = true   # Set to false for public access
```

Deploy:
```bash
cd mcps/github/deployment.legacy
terraform init
terraform apply -var-file=github-mcp.tfvars
```

Creates:
- ECR repository for Docker image
- App Runner service with VPC egress
- VPC ingress connection (if `mcp_github_is_private = true`)
- IAM roles for ECR access and CloudWatch logs
- Auto-scaling (min 1, max 2 instances)

**Private deployment** requires the main Bond AI deployment to have `has_private_mcp_services = true` (or `backend_is_private`/`frontend_is_private` set to `true`), which creates the shared `apprunner.requests` VPC endpoint.

> **Note**: If the main deployment's VPC endpoint is ever destroyed and recreated (e.g., toggling all private flags off then back on), you must re-apply this MCP deployment to update the ingress connection with the new endpoint ID.
