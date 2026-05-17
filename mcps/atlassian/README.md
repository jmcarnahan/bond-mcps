# Atlassian MCP Server for Bond AI

Custom MCP server providing Jira and Confluence tools for Bond AI. Built following the same patterns as the GitHub and Microsoft Graph MCP servers.

## Quick Start

```bash
# Install dependencies
cd mcps/atlassian
poetry install

# Run tests
poetry run pytest tests/ -v

# Start MCP server locally
fastmcp run atlassian_mcp.py --transport streamable-http --port 9001
```

## Tools (14 total)

### Jira (8 tools)

| Tool | Description |
|------|-------------|
| `list_projects` | List accessible Jira projects (name, key, type) |
| `search_issues` | Search with JQL — the workhorse tool |
| `count_issues` | Count issues matching JQL without fetching data |
| `get_issue` | Full issue details including comments |
| `create_issue` | Create issue with type, description, assignee, priority, labels |
| `update_issue` | Update summary, description, assignee, priority, labels |
| `add_issue_comment` | Add comment to an issue |
| `transition_issue` | Move issue through workflow (e.g., "In Progress", "Done") |

### Confluence (5 tools)

| Tool | Description |
|------|-------------|
| `list_spaces` | List accessible spaces |
| `search_content` | Search pages/blogs using CQL |
| `get_page` | Get page with body content |
| `create_page` | Create page in a space |
| `update_page` | Update page title/body |

### User (1 tool)

| Tool | Description |
|------|-------------|
| `get_myself` | Current user info (accountId, displayName, email) |

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
4. Set callback URL to your Bond AI backend OAuth callback
5. Note your Client ID and Client Secret

### 2. Find Your Cloud ID

```bash
# Replace YOUR-DOMAIN with your Atlassian domain
curl -s https://YOUR-DOMAIN.atlassian.net/_edge/tenant_info | jq .cloudId
```

## CLI Usage

The CLI uses environment variables for authentication (no OAuth flow):

```bash
export ATLASSIAN_ACCESS_TOKEN=your_token
export ATLASSIAN_CLOUD_ID=your_cloud_id

# Jira
atlassian-cli jira projects
atlassian-cli jira search "project = PROJ AND status = Open"
atlassian-cli jira count "project = PROJ AND type = Bug"
atlassian-cli jira get PROJ-123
atlassian-cli jira create PROJ "Fix login bug" --type Bug --priority High
atlassian-cli jira comment PROJ-123 "Working on this"
atlassian-cli jira transition PROJ-123 "In Progress"

# Confluence
atlassian-cli confluence spaces
atlassian-cli confluence search 'type = page AND text ~ "release notes"'
atlassian-cli confluence get 12345

# User
atlassian-cli user me
```

## Standalone Use with Claude Code

The MCP server can run standalone with local OAuth — no Bond AI backend required. Authentication uses browser-based authorization code + PKCE flow via a shared OAuth proxy.

### Prerequisites

1. An Atlassian OAuth 2.0 app (see [Atlassian OAuth App Setup](#atlassian-oauth-app-setup) above)
2. **Add a callback URL** to your OAuth app: `http://localhost:8000/connections/atlassian_v2/callback`

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
cd mcps/atlassian
poetry install

export ATLASSIAN_CLIENT_ID=<your-oauth-app-client-id>
export ATLASSIAN_CLIENT_SECRET=<your-oauth-app-client-secret>

# Optional: set cloud ID if you have multiple Atlassian sites
# Omit to auto-discover (prompts if multiple sites found)
export ATLASSIAN_CLOUD_ID=<your-cloud-id>

# Fails fast if the auth proxy isn't running
poetry run fastmcp run atlassian_mcp.py --transport streamable-http --port 9001
```

### Step 3: Register with Claude Code

```bash
claude mcp add-json atlassian '{"type":"http","url":"http://localhost:9001/mcp"}' --scope local
```

Then restart Claude Code to pick up the new server.

### Step 4: Authenticate

The first time you use an Atlassian tool in Claude Code, the server will open your browser to Atlassian's authorization page. After you authorize, the token is cached at `~/.bond_ai_tokens/atlassian.json`.

To force re-authentication:

```bash
rm ~/.bond_ai_tokens/atlassian.json
```

### Verify

Run `/mcp` in Claude Code to confirm `atlassian` shows as connected, then try:

> "What's my Atlassian profile?" or "List my Jira projects"

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

Create a tfvars file (e.g., `mcps/atlassian/deployment/atlassian-mcp.tfvars`):
```hcl
aws_region                 = "us-west-2"
environment                = "dev"
project_name               = "bond-ai"
existing_vpc_id            = "vpc-XXXXXXXXX"
mcp_atlassian_is_private   = true   # Set to false for public access
```

Deploy:
```bash
cd mcps/atlassian/deployment
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
The MCP server isn't receiving the OAuth token. Check that Bond AI backend is passing the `Authorization: Bearer` header.

### "Cloud ID required" error
The `X-Atlassian-Cloud-Id` header is missing. Ensure `cloud_id` is configured in the MCP server config.

### "Rate limited by Atlassian"
Atlassian enforces rate limits. The error message includes the retry-after time. Wait and try again.

### Transition fails with "not available"
The error will list available transitions. Use one of those names (case-insensitive).

### JQL syntax errors
The error message from Jira will include the specific JQL parsing error. Check [Jira JQL documentation](https://support.atlassian.com/jira-service-management-cloud/docs/use-advanced-search-with-jira-query-language-jql/).
