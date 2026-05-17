# Microsoft Graph MCP Server

MCP server providing Microsoft email, Teams, OneDrive, and SharePoint tools. Supports two authentication modes (resolved in this order, see `ms_graph/auth.py`):

- **Standalone**: Local MSAL OAuth (browser PKCE via the shared auth proxy, device-code fallback). Active when `MS_CLIENT_ID` is set. Used by the CLI and by MCP clients like Claude Code.
- **Backend mode**: Receives pre-authenticated Bearer tokens via the `Authorization` header (e.g. Bond AI forwarding a per-user token).

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

The CLI uses MSAL for authentication (browser-based PKCE flow with device code fallback). Tokens are cached locally at `~/.bond_mcps/microsoft.json`.

**Prerequisites:**
- The shared auth proxy must be running (`cd auth && poetry run python -m auth`)
- `MS_CLIENT_ID` set (Application ID from your Azure App Registration)
- `MS_CLIENT_SECRET` set if your Azure app is registered as a confidential client / Web platform. Without it, the token exchange returns `invalid_client`.
- `MS_TENANT_ID` set to your organizational tenant GUID if you need Teams, SharePoint, or Power BI. Defaults to `consumers` (personal accounts only).

```bash
export MS_CLIENT_ID=<your-application-client-id>
export MS_CLIENT_SECRET=<your-client-secret>   # if Azure app is confidential
```

The CLI is organized into subcategories: `whoami`, `email`, `calendar`, `teams`, `files`, `powerbi`. Run `poetry run ms-graph-cli <category> --help` for the full subcommand list.

```bash
# Profile
poetry run ms-graph-cli whoami

# Email
poetry run ms-graph-cli email list                                      # Recent inbox
poetry run ms-graph-cli email list --folder sentitems --top 20
poetry run ms-graph-cli email list --query "budget report"              # Search
poetry run ms-graph-cli email read <message_id>
poetry run ms-graph-cli email send user@example.com "Subject" "Body"
poetry run ms-graph-cli email send user@example.com "Subject" "Body" --from alias@outlook.com --cc cc@example.com

# Calendar
poetry run ms-graph-cli calendar list                                   # Next 7 days
poetry run ms-graph-cli calendar list --start 2026-06-01T00:00:00Z --end 2026-06-30T23:59:59Z
poetry run ms-graph-cli calendar get <event_id>
poetry run ms-graph-cli calendar create "Subject" 2026-06-15T14:00:00Z 2026-06-15T15:00:00Z --attendees a@example.com,b@example.com
poetry run ms-graph-cli calendar availability a@example.com,b@example.com 2026-06-15T09:00:00Z 2026-06-15T17:00:00Z

# Teams (organizational accounts only — set MS_TENANT_ID)
poetry run ms-graph-cli teams list                                      # Joined teams
poetry run ms-graph-cli teams list --team-id <team_id>                  # Channels in a team
poetry run ms-graph-cli teams chats --type oneOnOne
poetry run ms-graph-cli teams read --chat-id <chat_id>
poetry run ms-graph-cli teams send --chat-id <chat_id> "Hello!"
poetry run ms-graph-cli teams activity

# Files / OneDrive / SharePoint
poetry run ms-graph-cli files list                                      # OneDrive root
poetry run ms-graph-cli files list --path Documents --top 50
poetry run ms-graph-cli files list --query "quarterly report"           # Search across drives
poetry run ms-graph-cli files inspect <item_id>                         # Metadata only
poetry run ms-graph-cli files inspect <item_id> --content               # Also fetch text content
poetry run ms-graph-cli files sites                                     # Followed SharePoint sites
poetry run ms-graph-cli files sites --query engineering                 # Search sites
poetry run ms-graph-cli files list --site-id <site_id>                  # Files in a SharePoint site
poetry run ms-graph-cli files upload "notes.md" "# Hello" --folder Documents    # Create/overwrite text file
poetry run ms-graph-cli files copy <item_id> <dest_folder_id>
poetry run ms-graph-cli files rename <item_id> "new-name.txt"

# Power BI (organizational accounts only — separate scope token)
poetry run ms-graph-cli powerbi workspaces
poetry run ms-graph-cli powerbi content <workspace_id>
poetry run ms-graph-cli powerbi content <workspace_id> --type reports
poetry run ms-graph-cli powerbi query <workspace_id> <dataset_id> "EVALUATE TOPN(10, 'Sales')"
poetry run ms-graph-cli powerbi refresh <workspace_id> <dataset_id>
poetry run ms-graph-cli powerbi refresh <workspace_id> <dataset_id> --history
poetry run ms-graph-cli powerbi export <workspace_id> <report_id> --format PDF
```

Teams and SharePoint scopes (`Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Send`, `Sites.Read.All`) are only requested when `MS_TENANT_ID` is set, since consumer accounts don't support them. `Files.Read.All` is always requested (works with both consumer and organizational accounts). Power BI uses a separate scope (`https://analysis.windows.net/powerbi/api/.default`) and a separate token cache entry — it will trigger a fresh browser flow the first time you use it.

To clear cached tokens and re-authenticate:
```bash
rm -f ~/.bond_mcps/microsoft.json
```

Enable debug output to inspect token claims:
```bash
export MS_DEBUG=1
```

## Standalone Use with Claude Code

The MCP server runs standalone with local OAuth — no Bond AI backend required. MSAL browser-based PKCE flow via the shared OAuth proxy, with device code fallback for headless environments.

### Prerequisites

1. An Azure App Registration with the required API permissions (see [Azure App Registration](#azure-app-registration) above)
2. **Web redirect URI** on the app: `http://localhost:8000/connections/microsoft/callback`
3. **Client secret** created if your app is registered as a confidential client (`MS_CLIENT_SECRET`)
4. **Public client flows enabled** (optional — enables device code fallback for headless use)
5. `MS_CLIENT_ID` set in `mcps/microsoft/.env` (and `MS_CLIENT_SECRET` if confidential, `MS_TENANT_ID` if organizational)

### Recommended: orchestrate via the repo-root Makefile

```bash
make install            # one-time
make dev                # auth proxy on :8000 + Microsoft MCP on :18001 (and the other two)
make claude-add         # registers ms-graph / github / atlassian with Claude Code at user scope
make login-microsoft    # opens browser for first-time auth (or returns cached info)
```

`claude mcp list` should show `ms-graph` as ✓ Connected. Try in Claude Code:

> "What's my Microsoft profile?" or "List my recent emails"

### By hand

```bash
# Terminal 1 — auth proxy
cd auth && poetry run python -m auth

# Terminal 2 — MCP server
cd mcps/microsoft
poetry install
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 18001

# Register
claude mcp add --transport http --scope user ms-graph http://localhost:18001/mcp
```

### Authenticate / re-authenticate

Token cached at `~/.bond_mcps/microsoft.json` (MSAL-managed, shared with the CLI). MSAL handles silent refresh transparently using the cached refresh token.

```bash
make logout-microsoft      # or: rm ~/.bond_mcps/microsoft.json
make login-microsoft       # browser opens for re-auth
```

If the browser path fails (SSH, headless), MSAL falls back to device code flow — output goes to the server log (`tmp/logs/microsoft.log` when running via `make dev`).

## MCP Server

```bash
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 18001
```

### Available Tools (23)

| Tool | Description |
|------|-------------|
| `get_user_profile` | Get the authenticated user's profile information |
| `list_emails` | List recent emails or search email messages |
| `read_email` | Read a single email message by its ID |
| `send_email` | Send an email message |
| `list_calendar_events` | List calendar events in a date range |
| `get_calendar_event` | Get detailed information about a specific calendar event |
| `create_calendar_event` | Create a new calendar event |
| `check_availability` | Check free/busy availability for one or more people |
| `list_teams` | List joined Microsoft Teams, or list channels within a specific team |
| `list_chats` | List Teams chats (1:1, group, meeting) with last message preview |
| `read_teams_messages` | Read recent messages from a Teams channel or chat |
| `send_teams_message` | Send a message to a Teams channel or chat |
| `get_teams_activity` | Get recent Teams activity across all channels and chats as a CSV digest |
| `list_sharepoint_sites` | Search for SharePoint sites, or list followed sites |
| `list_files` | List or search files in OneDrive or SharePoint |
| `inspect_file` | Get metadata and optionally the content of a file from OneDrive or SharePoint |
| `upload_file` | Create or overwrite a text file in OneDrive or SharePoint |
| `copy_or_rename_file` | Copy or rename a file or folder |
| `list_powerbi_workspaces` | List all Power BI workspaces the user has access to |
| `list_powerbi_content` | List datasets, reports, and/or dashboards in a Power BI workspace |
| `query_dataset` | Execute a DAX query against a Power BI dataset and return results as CSV |
| `refresh_dataset` | Trigger an on-demand refresh of a Power BI dataset |
| `export_report` | Export a Power BI report to PDF, PNG, or PPTX and save it to OneDrive |

All parameters use simple `str`/`int` types for Bedrock compatibility. Teams tools return a friendly message when Teams is not available for the account (personal MSA accounts). File tools work with both OneDrive (consumer) and SharePoint (organizational). Power BI tools require an organizational tenant and use a separate token scope.

## Bond AI Integration

### 1. Add to BOND_MCP_CONFIG

Add a `microsoft` entry to the `mcpServers` object in your `BOND_MCP_CONFIG` environment variable:

```json
"microsoft": {
    "url": "http://localhost:18001/mcp",
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
poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port 18001
```

### 3. Connect via Bond AI UI

1. Restart the Bond AI backend (to load updated config)
2. In the Bond AI UI, go to **Connections** -- "Microsoft" will appear
3. Click **Connect** -> Microsoft login -> consent to permissions -> redirected back
4. Edit your agent -> select Microsoft tools (list_emails, send_email, etc.) -> save
5. Ask your agent: "do I have any emails?"

**Important**: After changing MCP tool selections on an agent, you must **save the agent** to update the Bedrock action groups. The tool-to-server mapping is baked into the action group at save time.

### 4. Production Deployment (AWS)

> ⚠️ **Legacy — inherited from `bond-ai`, not yet adapted to bond-mcps.**
> The Terraform in `mcps/microsoft/deployment.legacy/` still references the old
> `../../shared_auth/` paths and will fail `terraform apply` as-is. A shared
> deployment target (ECS Express / Fargate) is being designed at the top-level
> `deployment/` directory and will replace these per-MCP modules. Treat the
> sections below as reference only.

The Microsoft MCP server has its own Terraform module in `mcps/microsoft/deployment.legacy/` that deploys it as a standalone App Runner service.

#### Prerequisites

- AWS CLI configured with appropriate credentials
- Terraform >= 1.0 installed
- Docker running locally
- An Azure App Registration (see sections above and below)

#### Step 1: Deploy the MCP Server

Create a tfvars file (e.g., `mcps/microsoft/deployment.legacy/microsoft-mcp.tfvars`):
```hcl
aws_region                 = "us-west-2"
environment                = "dev"
project_name               = "bond-ai"
existing_vpc_id            = "vpc-XXXXXXXXX"
mcp_microsoft_is_private   = true   # Set to false for public access
```

Deploy:
```bash
cd mcps/microsoft/deployment.legacy
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
cd mcps/microsoft/deployment.legacy
terraform apply -var-file=microsoft-mcp.tfvars
```

Terraform detects code changes via file hashes and rebuilds the Docker image automatically.

To force a rebuild without code changes:
```bash
terraform apply -var-file=microsoft-mcp.tfvars -var="force_rebuild=$(date +%s)"
```

#### Tearing Down

```bash
cd mcps/microsoft/deployment.legacy
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
Start the shared auth proxy first: `cd auth && poetry run python -m auth`. The MCP server validates the proxy is reachable at startup when `MS_CLIENT_ID` is set.

### Claude Code: Browser auth succeeds but terminal hangs
Common causes: another service on port 8000, or the redirect URI `http://localhost:8000/connections/microsoft/callback` is not registered in the Azure app. To use a different port, set `BOND_AUTH_PROXY_PORT` before starting both the proxy and MCP server.

### Graph API returns 401 on mail endpoints
If `/me` works but `/me/messages` returns 401, you may be authenticated as a guest user in an Azure AD tenant rather than as the mailbox owner. Use the `consumers` authority for personal accounts, or your organization's tenant ID for corporate accounts.

### Bond AI routes tool to wrong server
If the backend log shows the tool being executed against the wrong MCP server (wrong hash), re-save the agent in the UI. The tool-to-server hash mapping is written into the Bedrock action group at agent save time and needs to be refreshed after config changes.

### "AuthorizationRequiredError" in backend logs
The user hasn't connected their Microsoft account yet. They need to go to Connections in the UI and click Connect for Microsoft.
