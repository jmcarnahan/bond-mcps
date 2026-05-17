# Microsoft Graph MCP Server

MCP server providing Microsoft email, Teams, OneDrive, and SharePoint tools. Supports two authentication modes:

- **Bond AI backend**: Receives pre-authenticated Bearer tokens (production deployment)
- **Local auth**: Browser-based OAuth with PKCE for standalone use with Claude Code, other MCP clients, or the CLI

## Quick Start

```bash
cd mcps/microsoft
poetry install

# Run tests (all mocked, no credentials needed)
poetry run pytest tests/ -v
```

## Azure App Registration

### Step 1: Register the Application

1. Go to **https://portal.azure.com** -> **Microsoft Entra ID** -> **App registrations** -> **New registration**
2. Fill in:
   - **Name**: Choose a name (avoid using "Microsoft" in the name -- Azure rejects it)
   - **Supported account types**: Choose based on your environment:
     - **Personal accounts only**: "Accounts in any organizational directory and personal Microsoft accounts"
     - **Corporate/single tenant**: "Accounts in this organizational directory only"
     - **Multi-tenant corporate**: "Accounts in any organizational directory"
   - **Redirect URI**: Platform = **Public client/native (mobile & desktop)**, URI = `http://localhost:8400`
3. Click **Register**
4. Note the **Application (client) ID** from the Overview page

### Step 2: Configure API Permissions

1. Go to **API permissions** -> **Add a permission** -> **Microsoft Graph** -> **Delegated permissions**
2. Add email permissions:
   - `Mail.Read` -- Read user mail
   - `Mail.ReadWrite` -- Read and write access to user mail
   - `Mail.Send` -- Send mail as a user
   - `MailboxSettings.Read` -- Read mailbox settings (needed to discover the correct sending address for consumer accounts)
   - `User.Read` -- Sign in and read user profile
   - `offline_access` -- Maintain access to data (enables refresh tokens)
3. Add file/SharePoint permissions:
   - `Files.Read.All` -- Read all files the user can access (OneDrive + SharePoint)
   - `Sites.Read.All` -- Read SharePoint sites (requires organizational account)
4. For Teams support (requires Microsoft 365 business or developer license):
   - `Team.ReadBasic.All` -- Read teams
   - `Channel.ReadBasic.All` -- Read channels
   - `ChannelMessage.Send` -- Send channel messages
5. Click **Add permissions**

**Corporate environments**: If permissions require admin consent, an Azure AD admin must click **Grant admin consent for [tenant]** on the API permissions page.

### Step 3: Enable Public Client Flows (optional, for device code fallback)

1. Go to **Authentication** -> scroll to **Advanced settings**
2. Set **Allow public client flows** to **Yes**
3. Click **Save**

This enables the device code flow (used as a fallback when the browser-based flow fails in headless environments). Not required if you only use the MCP server via Bond AI.

### Step 4: Create a Client Secret

1. Go to **Certificates & secrets** -> **New client secret**
2. Add a description and choose an expiration period
3. Copy the **Value** immediately (it is only shown once)

The client secret is used for the token exchange in both the Bond AI backend and standalone/Claude Code modes (set via `MS_CLIENT_SECRET`). If your app is registered without a secret (public client only), the server uses MSAL's `PublicClientApplication` with PKCE instead.

### Step 5: Add Web Redirect URI (for Bond AI integration)

1. Go to **Authentication** -> **Add a platform** -> **Web**
2. Redirect URI: `http://localhost:8000/connections/microsoft/callback`
   - For production, use your actual backend URL: `https://<your-backend>/connections/microsoft/callback`
3. Click **Configure**

You will have two redirect URIs configured:
- **Public client/native**: `http://localhost:8400` (for CLI / standalone MCP)
- **Web**: `http://localhost:8000/connections/microsoft/callback` (for Bond AI OAuth flow)

## Authority / Tenant Configuration

Microsoft OAuth uses an "authority" URL that determines which accounts can sign in. Choose based on your environment:

| Environment | Authority | Notes |
|-------------|-----------|-------|
| Personal accounts (Outlook.com, Hotmail) | `https://login.microsoftonline.com/consumers` | Email only, no Teams |
| Single corporate tenant | `https://login.microsoftonline.com/<TENANT_ID>` | Full M365 features |
| Any corporate tenant | `https://login.microsoftonline.com/organizations` | Multi-tenant apps |
| Corporate + personal | `https://login.microsoftonline.com/common` | Broadest access |

**Important**: Personal accounts (`consumers`) do not support Teams scopes. Teams requires a Microsoft 365 business, education, or developer license.

For the CLI, set the tenant via environment variable:
```bash
export MS_TENANT_ID=consumers          # Personal accounts
export MS_TENANT_ID=<your-tenant-id>   # Specific corporate tenant
```

If `MS_TENANT_ID` is not set, the CLI defaults to `consumers`.

## CLI Usage

The CLI uses MSAL for authentication (browser-based PKCE flow with device code fallback). Tokens are cached locally at `~/.ms_graph_tokens.json`.

```bash
export MS_CLIENT_ID=<your-application-client-id>

# User profile
poetry run python ms_graph_cli.py whoami                         # Show authenticated user info

# Email
poetry run python ms_graph_cli.py list                           # List inbox
poetry run python ms_graph_cli.py list --folder sentitems        # List sent items
poetry run python ms_graph_cli.py list --top 20                  # More results
poetry run python ms_graph_cli.py read <message_id>              # Read a single email
poetry run python ms_graph_cli.py send user@example.com "Subject" "Body"
poetry run python ms_graph_cli.py send user@example.com "Subject" "Body" --from alias@outlook.com
poetry run python ms_graph_cli.py search "budget report"

# Teams (requires M365 license -- set MS_TENANT_ID to your org tenant)
export MS_TENANT_ID=<your-tenant-id>
poetry run python ms_graph_cli.py teams list
poetry run python ms_graph_cli.py teams channels <team_id>
poetry run python ms_graph_cli.py teams send <team_id> <channel_id> "Hello!"

# Files / OneDrive
poetry run python ms_graph_cli.py files list                        # List OneDrive root
poetry run python ms_graph_cli.py files list --path Documents       # List subfolder
poetry run python ms_graph_cli.py files info <item_id>              # File metadata
poetry run python ms_graph_cli.py files read <item_id>              # Read text file content
poetry run python ms_graph_cli.py files search "quarterly report"   # Search across drives

# SharePoint sites
poetry run python ms_graph_cli.py sites list --query engineering    # Search for sites
poetry run python ms_graph_cli.py sites list                        # List followed sites
poetry run python ms_graph_cli.py sites files <site_id>             # List site files
poetry run python ms_graph_cli.py sites files <site_id> --path "Shared Documents"
```

Teams and SharePoint scopes (`Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Send`, `Sites.Read.All`) are only requested when `MS_TENANT_ID` is set, since consumer accounts don't support them. `Files.Read.All` is always requested (works with both consumer and organizational accounts).

To clear cached tokens and re-authenticate:
```bash
rm -f ~/.ms_graph_tokens.json
```

Enable debug output to inspect token claims:
```bash
export MS_DEBUG=1
```

## Standalone Use with Claude Code

The MCP server can run standalone with local OAuth — no Bond AI backend required. Authentication uses browser-based authorization code + PKCE flow via a shared OAuth proxy, with device code as a fallback for headless environments.

### Prerequisites

1. An Azure App Registration with the required API permissions (see [Azure App Registration](#azure-app-registration) above)
2. **Add a Web redirect URI**: `http://localhost:8000/connections/microsoft/callback`
3. **Create a client secret** if your app is registered as a confidential client
4. **Enable public client flows** (optional, enables device code fallback)

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
cd mcps/microsoft
poetry install

export MS_CLIENT_ID=<your-application-client-id>
export MS_CLIENT_SECRET=<your-client-secret>

# Optional: for organizational accounts (Teams, SharePoint)
export MS_TENANT_ID=<your-tenant-id>

# Fails fast if the auth proxy isn't running
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 5557
```

### Step 3: Register with Claude Code

```bash
claude mcp add-json ms-graph '{"type":"http","url":"http://localhost:5557/mcp"}' --scope local
```

Then restart Claude Code to pick up the new server.

### Step 4: Authenticate

The first time you use a Microsoft tool in Claude Code, the server will open your browser to Microsoft's login page. After you sign in, the token is cached at `~/.ms_graph_tokens.json`. Subsequent calls use the cached token automatically.

If the browser doesn't open (SSH, headless), the server falls back to device code flow.

To force re-authentication:

```bash
rm ~/.ms_graph_tokens.json
```

### Verify

Run `/mcp` in Claude Code to confirm `ms-graph` shows as connected, then try:

> "What's my Microsoft profile?" or "List my recent emails"

## MCP Server

```bash
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 5557
```

### Available Tools

| Tool | Description |
|------|-------------|
| `get_user_profile` | Get the authenticated user's profile (name, email addresses, mailbox address) |
| `list_emails` | List recent emails from a mailbox folder |
| `read_email` | Read a single email by ID |
| `send_email` | Send an email message |
| `search_emails` | Search emails by keyword query |
| `list_teams` | List joined Microsoft Teams |
| `list_team_channels` | List channels in a team |
| `send_teams_message` | Send a message to a Teams channel |
| `list_onedrive_files` | List files/folders in OneDrive |
| `get_file_info` | Get detailed metadata for a file or folder |
| `read_file_content` | Read text file content (up to 512 KB) |
| `search_files` | Search files across OneDrive and SharePoint |
| `list_sharepoint_sites` | Search or list followed SharePoint sites |
| `list_site_files` | List files in a SharePoint site's document library |

All parameters use simple `str`/`int` types for Bedrock compatibility. Teams tools return a friendly message when Teams is not available for the account. File tools work with both OneDrive (consumer) and SharePoint (organizational) accounts.

## Bond AI Integration

### 1. Add to BOND_MCP_CONFIG

Add a `microsoft` entry to the `mcpServers` object in your `BOND_MCP_CONFIG` environment variable:

```json
"microsoft": {
    "url": "http://localhost:5557/mcp",
    "auth_type": "oauth2",
    "transport": "streamable-http",
    "display_name": "Microsoft",
    "description": "Connect to Microsoft email, Teams, OneDrive, and SharePoint",
    "oauth_config": {
        "provider": "microsoft",
        "client_id": "<AZURE_APP_CLIENT_ID>",
        "client_secret": "<AZURE_APP_CLIENT_SECRET>",
        "authorize_url": "https://login.microsoftonline.com/<AUTHORITY>/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/<AUTHORITY>/oauth2/v2.0/token",
        "scopes": "Mail.Read Mail.ReadWrite Mail.Send User.Read offline_access Files.Read.All Sites.Read.All",
        "redirect_uri": "http://localhost:8000/connections/microsoft/callback"
    }
}
```

Replace `<AUTHORITY>` with the appropriate value:
- `consumers` for personal Microsoft accounts
- Your tenant ID for corporate environments (e.g., `contoso.onmicrosoft.com` or a GUID)
- `common` for multi-tenant + personal

For Teams support, add Teams scopes to the `scopes` field:
```
"scopes": "Mail.Read Mail.ReadWrite Mail.Send User.Read offline_access Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Send"
```

### 2. Start the MCP Server

```bash
cd mcps/microsoft
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 5557
```

### 3. Connect via Bond AI UI

1. Restart the Bond AI backend (to load updated config)
2. In the Bond AI UI, go to **Connections** -- "Microsoft" will appear
3. Click **Connect** -> Microsoft login -> consent to permissions -> redirected back
4. Edit your agent -> select Microsoft tools (list_emails, send_email, etc.) -> save
5. Ask your agent: "do I have any emails?"

**Important**: After changing MCP tool selections on an agent, you must **save the agent** to update the Bedrock action groups. The tool-to-server mapping is baked into the action group at save time.

### 4. Production Deployment (AWS)

The Microsoft MCP server has its own Terraform module in `mcps/microsoft/deployment/` that deploys it as a standalone App Runner service.

#### Prerequisites

- AWS CLI configured with appropriate credentials
- Terraform >= 1.0 installed
- Docker running locally
- An Azure App Registration (see sections above and below)

#### Step 1: Deploy the MCP Server

Create a tfvars file (e.g., `mcps/microsoft/deployment/microsoft-mcp.tfvars`):
```hcl
aws_region                 = "us-west-2"
environment                = "dev"
project_name               = "bond-ai"
existing_vpc_id            = "vpc-XXXXXXXXX"
mcp_microsoft_is_private   = true   # Set to false for public access
```

Deploy:
```bash
cd mcps/microsoft/deployment
terraform init
terraform apply -var-file=microsoft-mcp.tfvars
```

After deployment, get the MCP endpoint:
```bash
terraform output mcp_microsoft_mcp_endpoint
# Public:  https://abc123xyz.us-west-2.awsapprunner.com/mcp
# Private: https://xyz789abc.us-west-2.awsapprunner.com/mcp (VPC ingress domain)
```

**Private deployment** (`mcp_microsoft_is_private = true`) requires the main Bond AI deployment to have `has_private_mcp_services = true` (or `backend_is_private`/`frontend_is_private` set to `true`), which creates the shared `apprunner.requests` VPC endpoint. The MCP service looks up this existing endpoint and creates its own VPC ingress connection.

> **Note**: If the main deployment's VPC endpoint is ever destroyed and recreated (e.g., toggling all private flags off then back on), you must re-apply this MCP deployment to update the ingress connection with the new endpoint ID.

#### Step 2: Configure the Azure App Redirect URI

Add the **production** redirect URI to the Azure App Registration:

1. Go to **Azure Portal** -> **Microsoft Entra ID** -> **App registrations** -> your app
2. Go to **Authentication** -> **Web** platform
3. Add redirect URI: `https://<YOUR_BACKEND_URL>/connections/microsoft/callback`
   - Example: `https://2ktjnesdym.us-west-2.awsapprunner.com/connections/microsoft/callback`
4. Click **Save**

#### Step 3: Update Bond AI Backend Config

Add the Microsoft MCP server to `bond_mcp_config` in your main deployment tfvars (`deployment/terraform-existing-vpc/environments/us-west-2-existing-vpc.tfvars`).

**Where does the tenant go?** The MCP server itself does not need a tenant ID -- it receives pre-authenticated Bearer tokens from the Bond AI backend. The tenant/authority is configured in the `authorize_url` and `token_url` fields of the `oauth_config` below. Replace `<AUTHORITY>` with the appropriate value:

| Environment | `<AUTHORITY>` value | Notes |
|-------------|---------------------|-------|
| Single corporate tenant | Your Azure AD tenant ID (GUID) | e.g., `a1b2c3d4-...` |
| Any corporate tenant | `organizations` | Multi-tenant apps |
| Corporate + personal | `common` | Broadest access |
| Personal only | `consumers` | No Teams support |

For a corporate deployment, your Azure AD admin will provide the tenant ID (a GUID like `a1b2c3d4-e5f6-7890-abcd-ef1234567890`). You can also find it on the App Registration **Overview** page as "Directory (tenant) ID".

Example `bond_mcp_config` entry (add alongside existing entries like `sbel`):

```json
"microsoft": {
    "url": "https://<MCP_SERVICE_URL>/mcp",
    "auth_type": "oauth2",
    "transport": "streamable-http",
    "display_name": "Microsoft",
    "description": "Connect to Microsoft email, Teams, OneDrive, and SharePoint",
    "oauth_config": {
        "provider": "microsoft",
        "client_id": "<AZURE_APP_CLIENT_ID>",
        "client_secret": "<AZURE_APP_CLIENT_SECRET>",
        "authorize_url": "https://login.microsoftonline.com/<AUTHORITY>/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/<AUTHORITY>/oauth2/v2.0/token",
        "scopes": "Mail.Read Mail.ReadWrite Mail.Send MailboxSettings.Read User.Read offline_access Files.Read.All Sites.Read.All Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Send",
        "redirect_uri": "https://<YOUR_BACKEND_URL>/connections/microsoft/callback"
    }
}
```

Replace:
- `<MCP_SERVICE_URL>` -- from `terraform output mcp_microsoft_mcp_endpoint`
- `<AZURE_APP_CLIENT_ID>` -- from Azure App Registration Overview
- `<AZURE_APP_CLIENT_SECRET>` -- from Azure App Registration Certificates & secrets
- `<AUTHORITY>` -- tenant ID or `common`/`organizations`/`consumers`
- `<YOUR_BACKEND_URL>` -- your Bond AI backend URL

**Tip**: For secrets in production, store the client secret in AWS Secrets Manager and reference it via `client_secret_arn` instead of inline `client_secret`.

#### Step 4: Re-deploy the Bond AI Backend

```bash
cd deployment/terraform-existing-vpc
terraform apply -var-file=environments/us-west-2-existing-vpc.tfvars
```

#### Step 5: Connect and Test

1. In the Bond AI UI, go to **Connections** -- "Microsoft" will appear
2. Click **Connect** -> Microsoft login -> consent to permissions
3. Edit your agent -> select Microsoft tools -> **Save**
4. Ask your agent: "list my emails"

#### Updating the MCP Server

To rebuild and redeploy after code changes:
```bash
cd mcps/microsoft/deployment
terraform apply -var-file=microsoft-mcp.tfvars
```

Terraform detects code changes via file hashes and rebuilds the Docker image automatically.

To force a rebuild without code changes:
```bash
terraform apply -var-file=microsoft-mcp.tfvars -var="force_rebuild=$(date +%s)"
```

#### Tearing Down

```bash
cd mcps/microsoft/deployment
terraform destroy -var-file=microsoft-mcp.tfvars
```

This removes the App Runner service, ECR repository, IAM roles, VPC connector, VPC ingress connection (if private), and security group. It does not affect the Bond AI backend or any other infrastructure.

## For IT / Azure AD Administrators

This section is for the Microsoft 365 / Azure AD administrator who needs to set up the Azure App Registration for Bond AI's Microsoft integration.

### What Bond AI Needs

Bond AI connects to Microsoft Graph on behalf of each user (delegated permissions). It does **not** use application-level access -- each user authenticates individually and can only access their own email and Teams.

### What to Create

**1. Register a new application in Microsoft Entra ID**

- Go to **https://portal.azure.com** -> **Microsoft Entra ID** -> **App registrations** -> **New registration**
- **Name**: e.g., "Bond AI" (avoid using "Microsoft" in the name)
- **Supported account types**: "Accounts in this organizational directory only" (single tenant)
- **Redirect URI**: Leave blank for now (added in step 3)
- Click **Register**

**2. Configure delegated API permissions**

Go to **API permissions** -> **Add a permission** -> **Microsoft Graph** -> **Delegated permissions**:

| Permission | Why it's needed |
|------------|-----------------|
| `User.Read` | Sign in and read user profile |
| `Mail.Read` | List and read emails |
| `Mail.ReadWrite` | Manage email (move, mark as read) |
| `Mail.Send` | Send email on behalf of the user |
| `MailboxSettings.Read` | Read mailbox settings (discovers correct sending address for consumer accounts) |
| `offline_access` | Refresh tokens (keeps sessions alive without re-login) |
| `Files.Read.All` | Read files the user can access (OneDrive + SharePoint) |
| `Sites.Read.All` | Read SharePoint sites the user can access |
| `Team.ReadBasic.All` | List Teams the user has joined |
| `Channel.ReadBasic.All` | List channels in a Team |
| `ChannelMessage.Send` | Send messages to Teams channels |

After adding permissions, click **Grant admin consent for [your tenant]** if your organization requires admin consent for these permissions.

**Note**: All permissions are **delegated** (user-level). Bond AI never accesses data without the user being signed in. Omit the Teams permissions if Teams integration is not needed. Omit Files/Sites permissions if file access is not needed.

**3. Add a redirect URI**

Go to **Authentication** -> **Add a platform** -> **Web**:
- **Redirect URI**: `https://<BOND_AI_BACKEND_URL>/connections/microsoft/callback`
  - The Bond AI deployment team will provide this URL
  - Example: `https://2ktjnesdym.us-west-2.awsapprunner.com/connections/microsoft/callback`
- Click **Configure**

**4. Create a client secret**

Go to **Certificates & secrets** -> **Client secrets** -> **New client secret**:
- **Description**: e.g., "Bond AI production"
- **Expires**: Choose based on your org's policy (recommended: 12 or 24 months)
- **Copy the Value immediately** -- it is only shown once

**5. Provide these values to the Bond AI deployment team**

| Value | Where to find it |
|-------|------------------|
| **Application (client) ID** | App Registration -> Overview |
| **Directory (tenant) ID** | App Registration -> Overview |
| **Client secret value** | From step 4 (copy immediately) |

The deployment team does **not** need admin access to your Azure AD tenant.

### Security Notes

- Bond AI uses the **OAuth 2.0 authorization code flow with PKCE** -- the most secure OAuth flow available
- Each user must individually consent to permissions via Microsoft's login page
- Access tokens are short-lived (~1 hour); refresh tokens are encrypted at rest in Bond AI's database
- Bond AI does not store passwords or have access to any user's credentials
- The application does not have any **application-level** permissions -- it cannot access data without a signed-in user
- The client secret is used only for the server-side token exchange (confidential client flow)
- To revoke access for a user, the user can go to https://myapps.microsoft.com or an admin can revoke consent in Entra ID -> Enterprise applications

### Optional: Restrict to Specific Users

By default, all users in the tenant can consent to the application. To restrict access:

1. Go to **Microsoft Entra ID** -> **Enterprise applications** -> find the Bond AI app
2. Go to **Properties** -> set **Assignment required?** to **Yes**
3. Go to **Users and groups** -> add specific users or groups who should have access

## Architecture

```
Mode 1: Bond AI                    Mode 2: Claude Code / Standalone
========================           ===================================
User Browser                       Claude Code / MCP Client
    |                                   |
    v                                   v
Bond AI Frontend                   MCP Server (this project)
    |                                   |-- No Bearer header detected
    v                                   |-- MS_CLIENT_ID env var set
Bond AI Backend (FastAPI)               |-- local_auth.py:
    |-- OAuth flow                      |     1. Try cached token (silent)
    |-- Token pass-through              |     2. Browser PKCE flow
    |                                   |     3. Device code fallback
    v                                   |
MCP Server (this project)              v
    |-- Bearer token from header   Microsoft Graph API
    |
    v
Microsoft Graph API
```

**Mode 1 (Bond AI):** The MCP server does **not** manage OAuth. Bond AI's backend handles:
1. **Authorization**: Builds Microsoft OAuth URL with PKCE, redirects user to Microsoft login
2. **Token exchange**: Exchanges authorization code for access_token + refresh_token
3. **Token storage**: Encrypts and stores tokens in the database via `MCPTokenCache`
4. **Token refresh**: Automatically refreshes expired tokens using the refresh_token (enabled by `offline_access` scope)
5. **Token pass-through**: Sets `Authorization: Bearer <ms_graph_token>` header when calling the MCP server

The MCP server receives the token and uses it directly to call the Graph API. No token validation or JWT decoding is needed -- the Graph API validates the token itself.

**Mode 2 (Standalone):** When no Bearer header is present and `MS_CLIENT_ID` is set, the server authenticates directly using MSAL. If `MS_CLIENT_SECRET` is also set, it uses `ConfidentialClientApplication`; otherwise, it uses `PublicClientApplication`. Browser auth flows go through a shared OAuth callback proxy on `localhost:8000`. See [Standalone Use with Claude Code](#standalone-use-with-claude-code).

## Development

### Running Tests

```bash
poetry install
poetry run pytest tests/ -v
```

All tests use `respx` to mock HTTP calls to the Graph API. No Microsoft account or credentials needed.

### Project Structure

```
mcps/microsoft/
├── pyproject.toml           # Poetry project config
├── README.md                # This file
├── Dockerfile               # Container image for AWS deployment
├── .dockerignore            # Exclude tests, .env from Docker builds
├── .env.example             # Environment variable template
├── ms_graph_cli.py          # CLI tool (browser PKCE + device code fallback)
├── ms_graph_mcp.py          # MCP server (FastMCP)
├── ms_graph/
│   ├── __init__.py
│   ├── auth.py              # Token resolution (Bearer header or local MSAL)
│   ├── local_auth.py        # Local MSAL auth (browser PKCE + device code fallback)
│   ├── graph_client.py      # httpx-based Graph API client (sync + async)
│   ├── mail.py              # Mail operations (list, get, send, search)
│   ├── teams.py             # Teams operations (list teams, channels, send)
│   └── files.py             # File/drive operations (OneDrive + SharePoint)
├── deployment/              # Standalone Terraform module
│   ├── versions.tf          # Provider requirements
│   ├── variables.tf         # Shared + Microsoft-specific variables
│   ├── data-sources.tf      # VPC/subnet auto-discovery
│   ├── main.tf              # ECR, IAM, Docker build
│   ├── apprunner.tf         # App Runner service, VPC connector
│   └── outputs.tf           # Service URL, MCP endpoint
└── tests/
    ├── conftest.py          # Fixtures and mock Graph API responses
    ├── test_auth.py         # Token resolution tests (Bearer + local fallback)
    ├── test_local_auth.py   # Local MSAL auth tests (flows, scopes, cache)
    ├── test_graph_client.py # Client tests (auth headers, error handling)
    ├── test_mail.py         # Mail operation tests (sync + async)
    ├── test_teams.py        # Teams tests (sync + async + 403 handling)
    ├── test_files.py        # File/drive operation tests (sync + async)
    └── test_mcp_server.py   # MCP server integration tests
```

## Troubleshooting

### CLI: "No tenant-identifying information found"
Set `MS_TENANT_ID` explicitly. This happens when `common` authority can't determine the tenant.

### CLI: "The code you entered has expired"
Device codes expire after a few minutes. Run the command again and enter the code promptly. Use an incognito browser window to avoid cached login state.

### CLI: Teams scopes cause device flow failure
Consumer accounts don't support Teams scopes. Don't set `MS_TENANT_ID` (defaults to `consumers` which excludes Teams scopes), or set it to your organizational tenant ID.

### Claude Code: "AADSTS70002: The provided request must include a 'client_secret'"
Your Azure app is registered as a confidential client but `MS_CLIENT_SECRET` is not set. Export it before starting the MCP server:
```bash
export MS_CLIENT_SECRET=<your-client-secret>
```

### Claude Code: MCP server fails to start with "auth proxy is not running"
Start the shared auth proxy first: `cd mcps/shared_auth && poetry run python -m shared_auth`. The MCP server validates the proxy is reachable at startup when `MS_CLIENT_ID` is set.

### Claude Code: Browser auth succeeds but terminal hangs
Common causes: another service on port 8000, or the redirect URI `http://localhost:8000/connections/microsoft/callback` is not registered in the Azure app. To use a different port, set `BOND_AUTH_PROXY_PORT` before starting both the proxy and MCP server.

### Graph API returns 401 on mail endpoints
If `/me` works but `/me/messages` returns 401, you may be authenticated as a guest user in an Azure AD tenant rather than as the mailbox owner. Use the `consumers` authority for personal accounts, or your organization's tenant ID for corporate accounts.

### Bond AI routes tool to wrong server
If the backend log shows the tool being executed against the wrong MCP server (wrong hash), re-save the agent in the UI. The tool-to-server hash mapping is written into the Bedrock action group at agent save time and needs to be refreshed after config changes.

### "AuthorizationRequiredError" in backend logs
The user hasn't connected their Microsoft account yet. They need to go to Connections in the UI and click Connect for Microsoft.
