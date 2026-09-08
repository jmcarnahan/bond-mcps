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
   - `Files.ReadWrite` -- Write files to the user's OneDrive (needed to upload files, and to send files into Teams: Teams cannot carry file bytes on a message, so each one is uploaded to a drive first)
   - `Sites.Read.All` -- Read SharePoint sites (requires organizational account)
4. For Teams support (requires Microsoft 365 business or developer license):
   - `Team.ReadBasic.All` -- Read teams
   - `Channel.ReadBasic.All` -- Read channels
   - `ChannelMessage.Send` -- Send channel messages
   - `Chat.ReadWrite` -- Read, send, and create Teams chats (admin consent in most tenants)
5. For directory search (the recipient typeahead in the desktop client):
   - `User.ReadBasic.All` -- Read every user's basic profile; see "Directory search rollout" below for the consent order
6. Click **Add permissions**

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

## Mail sender policy

One environment variable, `MS_MAIL_ALLOWED_SENDER_DOMAINS`, hides mail that arrived from outside the organisation. Its value is a comma-separated list of domains, and it is both the toggle and the allowlist: unset or blank means the policy is **off** (the default, and every mail surface behaves exactly as it did before), and any non-empty list turns it **on**. Set it locally in `mcps/microsoft/.env` or in the shell — it applies to `make dev` and to the CLI alike — and in a deployment through the service's `extra_env` map in `deployment/terraform-existing-vpc/environments/<env>.tfvars` (see [docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md)).

**The rule.** A message is shown only when its Exchange `from` address belongs to an allowed domain and, when the message also carries a separate `sender` address (mail sent on behalf of someone, as an outside service does when it sends as a member of staff), that address belongs to an allowed domain too. Matching is on the exact domain, so subdomains must be listed in their own right. Anything the rule cannot judge is treated as external and hidden: a message with no `from`, and an address with no `@` in it such as a legacy X.500 address. The policy fails closed by design — an unrecognised shape is hidden rather than shown. The one exception is a draft: Graph returns no `from` on a draft at all, so a message Exchange marks `isDraft` is treated as the user's own composition and shown. Only Exchange sets that flag, on messages composed in the mailbox; a sender cannot deliver mail carrying it. Note that a reply draft composed in Outlook quotes the original, so such a draft is readable even when the message it replies to is hidden — see the scope statement below.

**Configuring it.** List every domain the organisation legitimately sends *from*: the primary domain, every alias domain, and the tenant's own `<tenant>.onmicrosoft.com`, which is the domain Exchange's system senders use for postmaster messages and non-delivery reports. Anything not listed is hidden, so software-as-a-service senders that send on behalf of staff (DocuSign, Jira and the like) and any shared or external support mailboxes disappear from every mail surface unless their domain is on the list. A malformed value — a bare word such as `example`, a `*`, an entry with an empty label — is a configuration error: the server refuses to start and every mail tool fails, rather than running unfiltered.

**What callers see.** Every tool returns `{"error": "external_sender"}`, which is a permanent error and must not be retried; the CLI prints the sentence "This message is from a sender outside the allowed domains and is hidden by the mail policy." instead, and that same sentence is the `reason` when a send tries to forward an attachment off a hidden message. `list_emails` returns that same constant notice as its `notice` key, and the CLI's `email list` appends it, whenever the policy is on, whether or not anything was actually hidden: "Messages from senders outside the allowed domains are hidden by the mail policy." No count of hidden messages is ever returned to a caller; that is deliberate, because a count against a `$search` query would let a caller probe the content of mail it cannot read. `connection_status` reports `mail_policy: {"enabled": true|false}`, and `{"enabled": true, "error": "invalid_config"}` when the configured value is malformed.

**Coverage.** Every surface that lists messages filters the list: `list_emails`, `sync_mail` (delta tombstones pass through unchanged so a client can still delete the rows they name), and the CLI's `email list`. Every surface that takes a message id verifies the sender before it does anything else with that message. `read_email` and the CLI's `email read` judge the message they have just fetched, before any mark-as-read write; `get_mail_attachment`, `manage_draft` with `action="reply"`, the forward-an-attachment spec in `send_email` and `send_teams_message`, and the CLI's `email attachment` first make one small `$select` request for the message's `from` and `sender`, against the same mailbox the read will use, and stop there if it is external. An attached message — an item attachment — is judged by the same rule before any of its content or bytes are returned; its file name still appears in an attachment listing, because the listing describes the parent message rather than the attached one. `manage_inbox_rules` refuses to create or update a rule that forwards or redirects mail while the policy is on, returning `{"error": "forwarding_rule"}` whose reason reads "Inbox rules that forward or redirect mail cannot be created while the mail policy is on.", because such a rule would re-deliver external mail into the mailbox as internal mail and quietly undo the policy.

**Deliberately not gated.**

- `mark_mail_read` and `list_emails`' `mark_as_read` — writes against ids the caller already holds, returning counts only.
- `manage_draft`'s `update_body`, `send`, and `add_attachment` actions — draft ids only, which Exchange rejects on anything that is not a draft — and its `create` action, which composes an outbound draft of the user's own and reads no message. Only its `reply` action is gated.
- `manage_mail_folders` — folder metadata, not mail.
- Teams, calendar, files, and Power BI tools — out of scope for a mail policy.

**Desktop clients must resync.** Turning the policy on, turning it off, or changing the list all require a client that follows the delta feed to resync. The policy filters pages as they are fetched; it does not reach into what a client already synced, so external messages a client stored before the policy was turned on stay there until it resyncs. In the other direction, messages already hidden do not reappear in a delta page when the policy is relaxed — only a fresh sync brings them back. A delta cursor issued before this policy shipped also encodes a `$select` without `sender`, so pages resumed from it are judged on `from` alone until the client resyncs.

**Scope of the control.** This control keys on Exchange's `from` and `sender`, and it stops agents reading mail that *arrived* from outside the allowed domains. It does not defeat a forged internal `From` that already passed the tenant's anti-spoofing — that is the mail gateway's job, through DMARC — and it does not cover content re-originated internally: a colleague's inline forward, a reply draft that already exists, a ticketing relay whose headers still name the original sender, an attachment previously saved to OneDrive, or an `.eml` posted in Teams. Not covered, as follow-ups: calendar invites from external organisers (the recommended next step), Teams messages from federated users, `.eml` files in Teams or OneDrive, and the hidden counts that remain inferable from a folder's `totalItemCount`.

### Rollout checklist

1. **Turn it on locally.** Add `MS_MAIL_ALLOWED_SENDER_DOMAINS=<primary-domain>,<tenant>.onmicrosoft.com` to `mcps/microsoft/.env`, restart `make dev`, and confirm the boot log line `Mail sender policy: on (2 allowed domain(s))`.
2. **Check the listings.** Run `list_emails` against the inbox: colleagues present, external senders absent, notice present. Repeat with `folder=drafts` and `folder=sentitems` and confirm your own items are still there. Non-delivery reports and postmaster items appear only when the `onmicrosoft.com` domain is listed.
3. **Check the id surfaces.** With the id of an external message noted before you turned the policy on: `read_email` returns `external_sender`, `get_mail_attachment` returns `external_sender`, `manage_draft` with `action="reply"` returns `external_sender`, and `manage_inbox_rules` refuses a create carrying `forwardTo`.
4. **Check the status probe.** `connection_status` reports `mail_policy.enabled == true`.
5. **Deploy it.** Add the same value to `ms_graph.extra_env` in `deployment/terraform-existing-vpc/environments/<env>.tfvars`, run `make deploy-plan` and then `make deploy`, confirm the same boot line in the pod log, repeat step 4 against the deployed server, and tell desktop-client users to resync.

## Directory search rollout

`search_people` needs `User.ReadBasic.All`, which the consented default scope set does not include, and `ensure_chat` needs `Chat.ReadWrite`. `get_profile` shares that requirement when it is given a `user` (the lookup and that user's photo); your own identity and photo need only `User.Read`. Without the scope it returns `directory_scope_missing` and nothing else. Both are admin-gated in an organisational tenant. The order below matters: Entra evaluates a consent request as one bundle, so widening `MS_SCOPES` before the admin has granted the scope walls every sign-in behind "Approval required", mail included (the PR #18 release note explains the rule).

1. **Admin consent first.** The Azure AD admin adds `User.ReadBasic.All` (delegated) — and `Chat.ReadWrite` if it is not there yet — on the app registration's API permissions and clicks **Grant admin consent for [tenant]**.
2. **Widen the requested scopes.** Append `User.ReadBasic.All` (and `Chat.ReadWrite`) to `MS_SCOPES` — in `mcps/microsoft/.env` locally, and in `ms_graph.extra_env` of `deployment/terraform-existing-vpc/environments/<env>.tfvars` for a deployment, then `make deploy-plan` and `make deploy`. Unset, `MS_SCOPES` falls back to the consented default in `ms_graph/local_auth.py`, so the full list must be written out.
3. **Every user reconnects.** A refresh token carries the scopes it was issued with, so an existing connection never gains the new ones. After reconnecting, `connection_status` lists `user.readbasic.all` (and `chat.readwrite`), which is what the desktop client keys its typeahead on.
4. **Verify.** `search_people(query="<a colleague's name prefix>")` returns people. Before step 3 it returns `{"error": "directory_scope_missing"}`, which is the expected answer rather than a fault; `ensure_chat` likewise returns `teams_unavailable` on a connection without `Chat.ReadWrite` — the same error a missing Teams licence produces, because both arrive as a Graph 403.

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

The CLI is organized into subcategories: `whoami`, `powerbi-whoami`, `email`, `rules`, `calendar`, `teams`, `files`, `powerbi`. Run `poetry run ms-graph-cli <category> --help` for the full subcommand list.

`email list`, `email read`, and `email attachment` honour `MS_MAIL_ALLOWED_SENDER_DOMAINS` exactly as the server does — see [Mail sender policy](#mail-sender-policy) above.

```bash
# Profile
poetry run ms-graph-cli whoami
poetry run ms-graph-cli whoami --user ada@example.com
poetry run ms-graph-cli whoami --user ada@example.com --photo ada.jpg --photo-size 96x96

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
poetry run ms-graph-cli teams search '#budget2026' --since 2026-01-01
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
poetry run ms-graph-cli files copy <item_id> "copy-of-report.docx" --dest-folder <folder_id>
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

### Available Tools (35)

| Tool | Description |
|------|-------------|
| `list_emails` | List recent emails or search email messages; optionally mark specific IDs as read; returns a `messages` table plus `count`, `folder`, `query`, `marked_read`, `notice` |
| `read_email` | Read a single email message by its ID; returns `subject`, `from_name`, `from_address`, `to`, `cc`, `received`, `is_read`, `is_draft`, `body_text`, `headers`, `has_attachments`, `attachments`, `attachment_count`, and `marked_as_read` when asked to mark; `options` take `mark_as_read`, `max_content_length`, `include_headers`, `include_inline`, `full_body` |
| `get_mail_attachment` | Read one email attachment's text, metadata, or bytes, or save it to OneDrive; returns the attachment summary plus what the mode adds |
| `send_email` | Send an email message, optionally with attachments; returns the ids the sent copy carries |
| `manage_inbox_rules` | Manage Outlook inbox rules: list, get, create, update, delete; returns a `rules` table, one `rule`, or the action taken and its id |
| `manage_mail_folders` | Manage Outlook mail folders: list, get, create, rename, move, delete; returns a `folders` table, one `folder`, or the action taken and its id |
| `list_calendar_events` | List calendar events in a date range; returns an `events` table plus `count` |
| `get_calendar_event` | Get detailed information about a specific calendar event; returns its `attendees` table plus the event's own fields |
| `create_calendar_event` | Create a new calendar event; returns its id, times, and links |
| `check_availability` | Check free/busy availability for one or more people; returns a `busy` table of every block, a per-person free-time `summary`, and `busy_count` |
| `list_teams` | List joined Microsoft Teams, or list channels within a specific team; returns a `teams` or `channels` table plus `count` |
| `list_chats` | List Teams chats (1:1, group, meeting), newest activity first; returns a `chats` table (unread, chat_type, topic, members, last_sender, last_preview, last_preview_at, last_read_at, id) plus `count`, `next_cursor`, and `marked_as_read` when the mark-as-read option was used |
| `read_teams_messages` | Read a Teams channel or chat: everything back to `since` (last 7 days by default), or ONE page newest-first in page mode; returns a `messages` table plus `count` and `next_cursor` |
| `search_teams_messages` | Search all Teams chats and channels for messages by hashtag or keyword; returns a `messages` table plus `count`, `query`, `since`, `conversation_id`, `skipped`, `notice` |
| `get_teams_attachment` | Read, download, thumbnail, or save to OneDrive a file, inline image, or card from a Teams message; `mode` is `text`, `metadata`, `bytes`, `onedrive`, or `thumbnail` (its size rides `options`), and every mode returns `kind`, `name`, `content_type`, and `size` plus what the mode adds |
| `send_teams_message` | Send a message to a Teams channel or chat, optionally with files and inline images; returns the created `message`, `sent_to`, and a `note` when mention_everyone was ignored |
| `get_teams_activity` | Get recent Teams activity across all channels and chats; returns an `activity` table plus `count`, `sources`, `hours` |
| `list_sharepoint_sites` | Search for SharePoint sites, or list followed sites; returns a `sites` table plus `count` and `query` |
| `list_files` | List or search files in OneDrive, SharePoint, or a sharing link; returns one `files` table plus `count`, `folder_path`, `query` |
| `edit_document` | Edit an existing Word document or Excel workbook in place; returns what was applied, the sheet names or the revision author, and the item's link |
| `manage_file` | Create, copy, rename, or delete a file or folder; returns the `action` taken and the item's id, name, and link |
| `list_powerbi` | List Power BI workspaces, or the datasets, reports, and dashboards in one; returns a `workspaces` or `items` table plus `count` |
| `query_dataset` | Execute a DAX query against a Power BI dataset; returns the result `rows` plus `count` |
| `refresh_dataset` | Trigger an on-demand refresh of a Power BI dataset; returns the acknowledgement |
| `export_report` | Export a Power BI report to PDF, PNG, or PPTX and save it to OneDrive; returns the file's size, `item_id`, and link |
| `get_profile` | Get the signed-in user's identity, or another directory user's, plus their profile photo: `user` targets an id or UPN, `photo` is `metadata` or `bytes` (sized by `photo_size`, default 240x240), and the photo keys are null when none is set |
| `search_people` | Search the organisation directory by name or mail prefix |
| `sync_mail` | Fetch one page of a mail folder's delta feed for incremental sync |
| `manage_draft` | Compose, edit, attach to, and send mail drafts: `action` is `create`, `reply`, `update_body`, `add_attachment`, or `send`; the draft actions return its ids, web link, and recipients, `update_body` returns `ok`, `add_attachment` returns `attachment_id`, and `send` returns the ids the sent copy carries |
| `mark_mail_read` | Mark messages read or unread in bulk, best effort per message |
| `get_chat_members` | List a chat's members (user IDs and display names) |
| `ensure_chat` | Find or create a Teams chat with one person, or create a group chat |
| `mark_chat_read` | Mark a Teams chat read for the signed-in user |
| `inspect_file` | Get a drive item's or sharing link's metadata, plus its text, its bytes, or a thumbnail: `mode` is `metadata`, `text`, `bytes`, or `thumbnail` (empty follows `read_content`), and the two binary modes add `content_base64` — the way a bare sharing URL becomes a preview |
| `connection_status` | Report whether Microsoft is connected, and with which scopes |

All parameters use simple `str`/`int` types for Bedrock compatibility. Teams tools return a permanent `teams_unavailable` (or `teams_not_available`) error when Teams is not available for the account (personal MSA accounts). File tools work with both OneDrive (consumer) and SharePoint (organizational). Power BI tools require an organizational tenant and use a separate token scope. Sending files into Teams uploads them to OneDrive first (chats: the `Microsoft Teams Chat Files` folder, shared read-only with the chat's members; channels: the channel's Files folder) and posts a file card that references them, so it needs the `Files.ReadWrite` permission. In an org tenant whose admin consented only `Files.Read.All`, file sends come back `files_scope_missing` while plain messages keep working.

`search_teams_messages` runs the Microsoft Search API over every chat and channel the user can see, then reads each hit in full, including replies inside channel threads. The index strips `#` from hashtags, so the tool searches the bare term and re-checks each message body for the literal `#tag` before returning it; several tags must all be present; plain keywords are stemmed by the index and not re-checked. `since` empty means all time (unlike `read_teams_messages`, which defaults to the last seven days), and `conversation_id` narrows the results to one chat or channel client-side, because the index has no conversation filter. It needs no new permissions: `Chat.Read` or `Chat.ReadWrite` together with `ChannelMessage.Read.All`. Search covers work and school accounts only; a personal account gets `search_unsupported` instead of results.

### Response formats and the desktop header

Every one of the 35 tools returns a canonical `dict` — no tool returns prose any
more — and that one dict serves both audiences. A caller sending the header
`X-Bond-Client: desktop` (the desktop mail client, which needs the cursors,
timestamps, and IDs it can act on) gets the dict as `structuredContent`. Every
other caller — the LLMs — gets a compact text rendering of the same dict, with
`structuredContent` omitted, because a client that has both channels forwards
only the structured one to the model and the verbose JSON would win. Parameters
stay `str`/`int` only, as everywhere else, with an empty string meaning
"absent".

The switch is `FormatNegotiation`, the middleware in the repo's shared
[`common/`](../../common) package (`bond_common`), and a tool opts into it by
declaring `output_schema=None`. That declaration is the signal because it is the
only one left: in fastmcp every tool produces structured content — even a
`-> str` tool, auto-wrapped as `{"result": ...}` behind a generated schema — so
the presence of structured content cannot tell the two surfaces apart, while the
advertised schema can.

The compact renderer applies five rules in order, and every tool docstring
documents which one it lands on:

1. **A per-tool renderer**, if the tool registered one — consulted before
   everything below, including the error rule.
2. **An error envelope** — any payload with an `error` key — renders as
   `key: value` lines with `error: <code>` first.
3. **A table** — one list of flat rows plus scalars — renders as pipe-CSV (a
   header row of the union of the row keys in first-seen order) with the
   remaining non-empty scalars as trailing `key: value` lines. An empty list
   renders as `<key>: (none)`.
4. **A record** — an all-scalar payload — renders as `key: value` lines.
5. **Anything still nested** falls back to compact JSON.

Three tools nest too deeply for the table rule and register a renderer of their
own: `read_teams_messages` and `send_teams_message`, whose message rows carry
their own attachment lists, and `read_email`, whose envelope, body, attachments,
and headers are four shapes in one payload. All three hand error payloads back
to the shared rules, since rule 1 runs ahead of rule 2 rather than after it.

#### What the merges changed

`send_email` now always sends through a draft, because Graph's `sendMail`
answers 202 with no body: creating the draft first is the only way to learn the
`internet_message_id` and `conversation_id` that identify the Sent Items copy.
`check_availability` lists every busy block it was told about rather than the
first ten. `query_dataset` returns the DAX rows exactly as Power BI sent them
instead of pre-formatting CSV; Power BI omits null-valued columns from a row,
and the shared renderer unions the row keys in first-seen order and renders the
omissions as empty cells. `manage_file` also fixed its error handling: it used
to catch every `GraphError` and turn it into a success-shaped string, so a
throttle or a 5xx read as a permanent answer. It now maps only 404 to
`not_found` and lets everything else propagate as a tool error, and
`edit_document` does the same on its item fetch.

Five sets of tools merged outright:

- **Power BI and files.** `list_powerbi` absorbed `list_powerbi_workspaces` and
  `list_powerbi_content`: an empty `workspace_id` lists the workspaces, and a
  workspace id — or `"me"` for My workspace — lists its contents with a `kind`
  column. `manage_file` absorbed `upload_file` as `action="upload"`, where
  `folder_path` and `site_id` move into `options`; base64 content still wins
  over the file extension, so a base64 `.docx` uploads its bytes rather than
  being generated from markdown.
- **Teams chats.** `list_chats` absorbed `list_chats_page`, gaining `cursor` and
  returning a `next_cursor`; `top` above 50 now pages internally (Graph caps
  `/me/chats` at 50 a page) where the json name simply errored, and the rows
  carry the member names, the unread flag, and the last preview the old markdown
  listing showed. `read_teams_messages` absorbed `list_chat_messages_page` and
  now has two modes: by default it paginates back to `since` filtering on
  creation time, and under `{"page": true}` — or any `cursor` — it returns ONE
  page, newest first, with `since` filtering last-modified time so an edited
  message resurfaces. That page mode is chats only. `send_teams_message`
  absorbed `send_chat_message_json`, taking the desktop's base64 file array as a
  top-level `attachments` parameter beside the source specs in `options`, and
  returning the created message rather than a sentence. Its `content_type`
  default changed from `auto` to `text` as part of the merge: a typed `<` must
  reach the chat as a `<`, and markup now needs an explicit
  `{"content_type": "html"}` or `{"content_type": "auto"}`.
- **Attachments.** `get_mail_attachment` absorbed `get_email_attachment` and
  `get_mail_attachment_json`: one tool with `mode=metadata|text|bytes|onedrive`,
  a `mailbox` for shared mailboxes, and `folder_path` / `site_id` in `options`.
  `get_teams_attachment` absorbed `get_chat_attachment_json`, gaining `metadata`
  (the only mode that describes a card or a quoted reference), `thumbnail`, the
  10 MB `bytes` cap, and structured error codes, while keeping the team/channel
  scope the json tool never had.
- **The mail reader.** `read_email` absorbed `get_mail_detail` and became a
  dict: its `body_text` is Exchange's server-converted `uniqueBody` — the
  reply-relevant part only, without the quoted thread — where the markdown
  version dumped a whole raw HTML body into the model's context, and the
  attachment list now rides the same `$expand` as the body instead of costing a
  second request (with it goes the old "could not list attachments" note, which
  had no failure left to report). `headers` is always in the dict;
  `include_headers` only decides whether the compact rendering prints it, as
  `max_content_length` and `include_inline` only decide how much of the body and
  which attachment rows are shown. `{"full_body": true}` swaps `body_text` to
  the whole thread, quoted history included, and is the only thing that asks
  Graph for it.
- **The draft flow.** `manage_draft` collapsed the five draft tools into one
  `action` word: `create`, `reply`, `update_body`, `add_attachment`, `send`. The
  frozen payloads survive unchanged at the new name; only `create_draft_json`'s
  `body` parameter was renamed to `text`, which is what `update_body` already
  called it. The mail sender policy gates exactly one of the five actions:
  `reply`, because Graph would quote a hidden original into a draft whose `from`
  is the user. The other four touch only the user's own outbound mail and are
  deliberately ungated.

#### Hidden aliases

Every old name still answers, but is tagged `deprecated-alias` and hidden from
`tools/list`, so a model never sees two names for one tool. They exist for the
desktop client, which migrates on its own schedule; they come out in a later
round once it has. Where an alias preserves a quirk that does not survive at the
new name, the quirk is named here and nowhere else.

| Old name (hidden) | Answers as | Quirk it preserves |
|---|---|---|
| `get_user_profile` | `get_profile` | |
| `get_profile_json` | `get_profile` | |
| `search_people_json` | `search_people` | |
| `list_mail_delta` | `sync_mail` | |
| `mark_mail_read_json` | `mark_mail_read` | |
| `get_chat_members_json` | `get_chat_members` | |
| `ensure_chat_json` | `ensure_chat` | |
| `mark_chat_read_json` | `mark_chat_read` | |
| `inspect_file_json` | `inspect_file` | |
| `list_powerbi_workspaces` | `list_powerbi` | no `workspace_id` parameter |
| `list_powerbi_content` | `list_powerbi` | |
| `upload_file` | `manage_file` | pins `action="upload"`; `folder_path` / `site_id` stay flat parameters |
| `get_email_attachment` | `get_mail_attachment` | `mode="base64"` still means `bytes` |
| `get_mail_attachment_json` | `get_mail_attachment` | `mode` still defaults to `bytes`, not `text` |
| `get_chat_attachment_json` | `get_teams_attachment` | `thumbnail` is still a flat parameter |
| `list_chats_page` | `list_chats` | |
| `list_chat_messages_page` | `read_teams_messages` | pins page mode (`{"page": true}`) |
| `send_chat_message_json` | `send_teams_message` | the prose errors `"chat_id must not be empty"` and `"text must not be empty"`, where the new name answers `invalid_arguments` |
| `get_mail_detail` | `read_email` | |
| `create_reply_draft_json` | `manage_draft` | pins `action="reply"` |
| `create_draft_json` | `manage_draft` | pins `action="create"`; its `body` parameter, renamed `text` at the new name |
| `update_draft_body` | `manage_draft` | pins `action="update_body"` |
| `add_draft_attachment_json` | `manage_draft` | pins `action="add_attachment"` |
| `send_draft` | `manage_draft` | pins `action="send"` |

#### Permanent errors

A missing Microsoft connection returns `{"error": "not_connected", "connect_url": ...}` rather than raising, so a client can render a connect prompt. `connect_url` is null in laptop (MSAL) mode, which has no per-user connect endpoint. Argument-validation failures come back as error dicts rather than prose — `invalid_options`, `invalid_action`, `invalid_arguments`, `invalid_attachments`, `invalid_date`, `missing_rule_id`, `missing_folder_id`, `no_data`, and `folder_not_found`. The Teams write tools (`mark_chat_read`, `send_teams_message`) return a structured `"teams_unavailable"` error (with a `reason` on `send_teams_message`, `list_chats`, and `read_teams_messages`) for the permanent no-Teams-license 403, which a client must not retry; `list_teams`, `get_teams_activity`, and `search_teams_messages` spell that same 403 `teams_not_available`, and `search_teams_messages` adds `search_unsupported` for the consumer accounts Microsoft Search does not index. The mail attachment surfaces (`get_mail_attachment`, `manage_draft` with `action="add_attachment"`) likewise return structured permanent errors — `invalid_mode`, `invalid_options`, `too_large`, `reference`, `empty_name`, `invalid_base64` — which a client must not retry either; `manage_draft` adds `invalid_action` for an unknown action word and `invalid_arguments` for a missing `draft_id` or `message_id`, both answered before any request; `get_mail_attachment` in `bytes` mode caps content at 10 MB and reports `too_large` above it, decided from the metadata so nothing is downloaded. The Teams attachment reader (`get_teams_attachment`) returns `not_found` (whose `available` list names the ids the message does carry), `access_denied`, `no_thumbnail`, `invalid_thumbnail`, `is_folder`, `invalid_arguments`, `teams_unavailable`, and `too_large` — it shares the same 10 MB cap, decided from the driveItem size before a file is downloaded — `send_teams_message` returns `invalid_attachments` (bad JSON or an entry missing `name`/`content_base64`) and `files_scope_missing` (the connection lacks `Files.ReadWrite`), `list_chats` and `read_teams_messages` return `no_identity` when a mark-as-read option cannot name the signed-in user and `read_teams_messages` adds `invalid_date` for a malformed `since`, and `inspect_file` returns `missing_target`, `invalid_options`, `invalid_mode`, `invalid_thumbnail`, `is_folder`, `no_thumbnail`, `too_large` (its `bytes` and `thumbnail` modes share the same 10 MB cap, decided from the driveItem size before anything is downloaded and re-checked on what arrived), `access_denied`, `not_found`, and `invalid_link`; all of these are permanent too. `list_files` maps an unusable sharing link to `access_denied`, `not_found`, or `invalid_link`; `manage_file` returns `not_found` for a missing item and `too_large` (with the byte `limit`) for upload content over the 4 MB simple-upload cap; `edit_document` returns `edit_failed` when a document rejects an edit operation. `search_people` returns `directory_scope_missing` when the connection lacks `User.ReadBasic.All`, and `get_profile` returns the same code — carrying a `reason` and returned alone, with no profile data and no photo keys — whenever a `user` lookup or a photo request meets that 403; `get_profile` also returns `user_not_found` for an id or UPN the directory does not know, `invalid_arguments` for a `user` value Graph cannot parse as either, and `invalid_photo` (a mode word other than `metadata` or `bytes`), `invalid_photo_size` (a size Graph does not serve, or any size without `photo="bytes"`), and `too_large` for an image over the 10 MB JSON cap. Meanwhile `ensure_chat` returns `invalid_members` (an id that is not a Graph user id or UPN), `no_identity` (the signed-in user cannot be read off the token), and `no_members` (nobody left after dropping blanks and the caller), as well as `teams_unavailable`; these are permanent as well. `manage_draft` with `action="send"` reads the draft's `conversation_id` and `internet_message_id` before it sends and returns them, so a client can store its own copy of the sent mail at once and match it to the Sent Items copy by `internet_message_id`. The three paging tools (`sync_mail`, `list_chats`, `read_teams_messages`) return `invalid_cursor` when the cursor they were given is not a Graph URL: cursors only ever come from those tools, and the server refuses to send the bearer token anywhere but Graph. `read_email`, `get_mail_attachment`, and `manage_draft` with `action="reply"` return `external_sender` as a dict when the mail sender policy hides the message, which is permanent as well, and `connection_status` reports the policy's state under `mail_policy` so a client can explain the refusal. Every other failure — throttling, Graph 5xx — propagates as a tool error, which the client reads as "transient, retry later".

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

Example `bond_mcp_config` entry:

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
| `Chat.ReadWrite` | Read, send, and create Teams chats |
| `User.ReadBasic.All` | Search the organisation directory (recipient typeahead), read another user's profile, and their profile photos (`get_profile` with `user`) |

After adding permissions, click **Grant admin consent for [your tenant]** if your organization requires admin consent for these permissions.

**Note**: All permissions are **delegated** (user-level). Bond AI never accesses data without the user being signed in. Omit the Teams permissions if Teams integration is not needed. Omit Files/Sites permissions if file access is not needed. Omit `User.ReadBasic.All` if directory search is not needed.

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
