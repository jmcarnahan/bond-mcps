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
poetry run fastmcp run github_mcp.py --transport streamable-http --port 5558
```

### Run Tests
```bash
poetry run pytest tests/ -v
```

### CLI (Device Code Flow)
```bash
export GH_CLIENT_ID=<your-client-id>
poetry run github-cli repos list
poetry run github-cli issues list <owner> <repo>
poetry run github-cli pulls list <owner> <repo>
poetry run github-cli user
```

## Standalone Use with Claude Code

The MCP server can run standalone with local OAuth — no Bond AI backend required. Authentication uses browser-based authorization code + PKCE flow via a shared OAuth proxy, with device code as a fallback for headless environments.

### Prerequisites

1. A GitHub OAuth App (see [GitHub OAuth App Setup](#github-oauth-app-setup) below)
2. **Add a callback URL**: `http://localhost:8000/connections/github/callback`

### Step 1: Start the Shared Auth Proxy

The OAuth callback proxy handles browser redirects for all MCP servers. Start it in its own terminal:

```bash
cd mcps/shared_auth
poetry install
poetry run python -m shared_auth
```

You should see `Bond AI OAuth Proxy — Listening on 127.0.0.1:8000`. Leave this running.

### Step 2: Start the MCP Server

In a second terminal:

```bash
cd mcps/github
poetry install

export GITHUB_CLIENT_ID=<your-oauth-app-client-id>
export GITHUB_CLIENT_SECRET=<your-oauth-app-client-secret>

# Fails fast if the auth proxy isn't running
poetry run fastmcp run github_mcp.py --transport streamable-http --port 5558
```

### Step 3: Register with Claude Code

```bash
claude mcp add-json github '{"type":"http","url":"http://localhost:5558/mcp"}' --scope local
```

Then restart Claude Code to pick up the new server.

### Step 4: Authenticate

The first time you use a GitHub tool in Claude Code, the server will open your browser to GitHub's authorization page. After you authorize, the token is cached at `~/.bond_ai_tokens/github.json`.

GitHub tokens are long-lived — no refresh needed. To force re-authentication:

```bash
rm ~/.bond_ai_tokens/github.json
```

### Verify

Run `/mcp` in Claude Code to confirm `github` shows as connected, then try:

> "What's my GitHub profile?" or "List my repositories"

## Bond AI Integration

### BOND_MCP_CONFIG (Local Development)
```json
{
  "mcpServers": {
    "github": {
      "url": "http://localhost:5558/mcp",
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

The MCP server does NOT handle OAuth directly. Bond AI's backend handles:
1. OAuth authorization redirect to GitHub
2. Token exchange (code → access token)
3. Token storage and forwarding

The MCP server receives the user's GitHub access token as an `Authorization: Bearer` header on each request.

GitHub OAuth tokens are long-lived (no refresh token, no expiry). Once authorized, the token works until the user revokes it.

## GitHub OAuth App Setup

1. Go to https://github.com/settings/developers → OAuth Apps → New OAuth App
2. Set callback URL to your backend's `/connections/github/callback`
3. Copy Client ID and generate a Client Secret
4. Store the secret in AWS Secrets Manager for production

## Deployment (AWS)

Create a tfvars file (e.g., `mcps/github/deployment/github-mcp.tfvars`):
```hcl
aws_region              = "us-west-2"
environment             = "dev"
project_name            = "bond-ai"
existing_vpc_id         = "vpc-XXXXXXXXX"
mcp_github_is_private   = true   # Set to false for public access
```

Deploy:
```bash
cd mcps/github/deployment
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
