# bond-mcps as an OAuth 2.1 Resource Server

This guide covers the multi-tenant deployment shape where each MCP behaves
as an OAuth 2.1 Resource Server and the bond-mcps Authorization Server
(part of the same `auth/` Python package) acts as the identity layer.

For laptop / single-tenant usage, do nothing — keep `BOND_MCPS_JWT_*` unset
and the existing `make dev` + `make login-*` flow is unchanged.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  bond-mcps Auth Service (auth/auth_server, default port 8001)   │
│  /.well-known/oauth-authorization-server (RFC 8414)             │
│  /.well-known/jwks.json                                         │
│  /oauth/authorize  /oauth/token  /oauth/register                │
└─────────────────────────────────────────────────────────────────┘
                              │ delegates upstream login
                              ▼
                  Cognito user pool  -or-  Okta org
                              │
                              ▼
                  Claude Code stores JWT in keychain
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  MCP Resource Servers (github / ms-graph / atlassian / dbx)  │
   │  FastMCP(auth=RemoteAuthProvider(JWTVerifier(...)))          │
   │  Authorization: Bearer <bond-mcps-jwt>                       │
   │  user_key = AccessToken.claims["sub"]                        │
   └──────────────────────────────────────────────────────────────┘
                              │
                              ▼
                Provider API call (GitHub / MS Graph / ...)
```

The bond-mcps Authorization Server is self-contained — bond-ai is not
required at any layer. If bond-ai wants to call bond-mcps, it does OAuth
against the same AS like any other client.

## Mode matrix

| Mode | Trigger | `user_key` | AS required? |
|---|---|---|---|
| Laptop single-user | `BOND_MCPS_JWT_*` unset | `BOND_MCPS_USER_ID` env or `getpass.getuser()` | No |
| Deployed multi-user | `BOND_MCPS_JWT_JWKS_URI` set | JWT `sub` claim | Yes |

## Required environment

### Authorization Server (port 8001)

| Var | Required | Notes |
|---|---|---|
| `BOND_MCPS_AS_ENABLED` | yes | Set to `1` |
| `BOND_MCPS_AS_BASE_URL` | yes | Public URL, e.g. `https://auth.example.com` |
| `BOND_MCPS_AS_PRIVATE_KEY_PEM` | one of these | RSA private key PEM (recommended for k8s secret mounts) |
| `BOND_MCPS_AS_PRIVATE_KEY_FILE` | one of these | Path to a PEM file on disk |
| `BOND_MCPS_AS_PREVIOUS_KEY_PEM` | no | Optional overlap-window key during rotation |
| `BOND_MCPS_UPSTREAM_IDP` | yes | `cognito` or `okta` |
| `BOND_MCPS_UPSTREAM_ISSUER` | yes | OIDC issuer URL |
| `BOND_MCPS_UPSTREAM_CLIENT_ID` | yes | AS's own client ID at the upstream IdP |
| `BOND_MCPS_UPSTREAM_CLIENT_SECRET` | yes | AS's own client secret |
| `BOND_MCPS_UPSTREAM_REDIRECT_URI` | yes | e.g. `https://auth.example.com/oauth/upstream/callback` |
| `BOND_MCPS_UPSTREAM_SCOPES` | no | Default `openid email profile` |
| `BOND_MCPS_UPSTREAM_ALLOWED_DOMAINS` | no | CSV of email domains to gate sign-up |
| `BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS` | no | CSV of allowed redirect hosts (in addition to loopback) |

### Each MCP Resource Server

| Var | Required | Notes |
|---|---|---|
| `BOND_MCPS_JWT_JWKS_URI` | yes (prod) | `${AS_BASE}/.well-known/jwks.json` |
| `BOND_MCPS_JWT_PUBLIC_KEY` | alternative | Static PEM or HS256 shared secret (for tests) |
| `BOND_MCPS_JWT_ALGORITHM` | no | Default `RS256` |
| `BOND_MCPS_JWT_ISSUER` | yes | `${BOND_MCPS_AS_BASE_URL}` |
| `BOND_MCPS_JWT_AUDIENCE` | optional | Friendly audience name (CSV). Auto-merged with the canonical PRM URI (`${BOND_MCPS_PUBLIC_URL}/mcp`) so the JWT `aud` claim Claude Code passes (per RFC 8707) is always accepted. Leave unset unless you need a non-URI audience for compatibility. |
| `BOND_MCPS_JWT_SUB_CLAIM` | no | Default `sub` |
| `BOND_MCPS_AS_BASE_URL` | yes | Surfaced in protected-resource-metadata |
| `BOND_MCPS_PUBLIC_URL` | yes | Public URL of *this* MCP |

`BOND_MCPS_USER_ID`, `BOND_MCPS_DB_URL`, `BOND_MCPS_ENCRYPTION_KEY` are
unchanged from single-tenant mode.

## Setting up Cognito (test environment)

1. Create a user pool. Note the user pool ID and region.
2. In **App integration > App clients**, add a confidential client for the
   bond-mcps AS. Enable **Authorization code grant** with **PKCE**, and set
   the callback URL to `${BOND_MCPS_AS_BASE_URL}/oauth/upstream/callback`.
   Generate a client secret.
3. In **App integration > Domain**, configure a Cognito hosted UI domain
   (or use the Cognito-issued one). The OIDC issuer URL is
   `https://cognito-idp.<region>.amazonaws.com/<user-pool-id>`.
4. Optionally enable a federated identity provider (Google, Okta-as-IdP)
   inside Cognito if you want users to sign in via Google.
5. Configure these env vars on the AS:
   ```
   BOND_MCPS_UPSTREAM_IDP=cognito
   BOND_MCPS_UPSTREAM_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xxxxxxxxx
   BOND_MCPS_UPSTREAM_CLIENT_ID=<app client id>
   BOND_MCPS_UPSTREAM_CLIENT_SECRET=<app client secret>
   BOND_MCPS_UPSTREAM_REDIRECT_URI=https://auth.example.com/oauth/upstream/callback
   BOND_MCPS_UPSTREAM_SCOPES=openid email profile
   ```

## Setting up Okta

1. Create an OIDC app in Okta of type **Web application**.
2. Set the sign-in redirect URI to
   `${BOND_MCPS_AS_BASE_URL}/oauth/upstream/callback`.
3. Set **Client authentication** to **Client secret** (confidential). Enable
   the **Authorization Code** grant. PKCE may be optional or required —
   either works.
4. Pick or create an Okta authorization server. The issuer is
   `https://<org>.okta.com/oauth2/default` or `.../oauth2/<custom_id>`.
5. Env config on the AS:
   ```
   BOND_MCPS_UPSTREAM_IDP=okta
   BOND_MCPS_UPSTREAM_ISSUER=https://your-org.okta.com/oauth2/default
   BOND_MCPS_UPSTREAM_CLIENT_ID=<okta app client id>
   BOND_MCPS_UPSTREAM_CLIENT_SECRET=<okta app client secret>
   BOND_MCPS_UPSTREAM_REDIRECT_URI=https://auth.example.com/oauth/upstream/callback
   ```

## Local-dev (multitenant) walkthrough

Use the `make dev-multitenant` target for a hermetic local stack that
exercises the JWT-mode code path without standing up Cognito/Okta:

```bash
make dev-multitenant          # boots auth-proxy (8000), AS (8001), 4 MCPs
make status-mt                # confirms all 6 services are up
```

`/oauth/authorize` will return 500 until you provide upstream IdP config,
but you can still:

* `curl http://localhost:8001/.well-known/oauth-authorization-server`
* `curl http://localhost:8001/.well-known/jwks.json`
* `curl http://localhost:18002/.well-known/oauth-protected-resource/mcp`
* `curl http://localhost:18002/mcp -X POST ...` returns `401 Bearer error=...`
* Hand-craft a JWT with the local AS keypair (`~/.bond_mcps/jwt_signing_key.pem`)
  and use it as `Authorization: Bearer <jwt>` for end-to-end validation
  against the MCPs.

## Connecting Claude Code

```bash
claude mcp add --transport http \
  --client-id <client_id-from-DCR-or-static-config> \
  --callback-port 18999 \
  github https://github-mcp.example.com/mcp
```

When the server is reachable, run `/mcp` inside Claude Code; the OAuth
discovery + PKCE round-trip happens automatically. Tokens are persisted
in the workstation's keychain (macOS) or `~/.claude/.credentials.json`
(Linux), so subsequent sessions don't re-prompt.

### When Dynamic Client Registration is enabled

The AS exposes `POST /oauth/register` (RFC 7591). Claude Code v2.x will
register a client on first use automatically. No `--client-id` flag
needed.

### When you prefer a static client_id

Seed a static client via the `BOND_MCPS_STATIC_CLIENTS` env on the AS:

```bash
BOND_MCPS_STATIC_CLIENTS='[
  {
    "client_id": "bm-claude-code",
    "client_name": "Claude Code",
    "redirect_uris": ["http://127.0.0.1:18999/callback"]
  }
]'
```

Then pass `--client-id bm-claude-code --callback-port 18999` to
`claude mcp add`.

## Provider token bootstrap (`/connect/<provider>`)

When a tool call needs an upstream provider access token that the user
hasn't yet authorized, the MCP raises `MissingProviderConnection` which
surfaces as a tool error containing a `connect_url`. The user opens that
URL in a browser to complete the provider OAuth flow; the resulting
access token is written to `tokens.db` keyed by the JWT-derived
`user_key`. Subsequent tool calls succeed without further prompting.

GitHub and Atlassian have the connect flow wired up via the generic
`auth.connect_routes` module. Microsoft and Databricks raise
`MissingProviderConnection` without a `connect_url` — operators must
provision tokens out-of-band until per-provider connect routes ship
(MSAL handshake for Microsoft, U2M browser flow for Databricks).

## Operational concerns

### Key rotation

The AS publishes whichever PEM is in `BOND_MCPS_AS_PRIVATE_KEY_PEM` as the
signing key, with a `kid` derived from SHA-256 over the public-key DER.
For a rolling rotation:

1. Generate the new keypair.
2. Set `BOND_MCPS_AS_PREVIOUS_KEY_PEM` to the *current* key.
3. Set `BOND_MCPS_AS_PRIVATE_KEY_PEM` to the *new* key.
4. Redeploy. JWKS now publishes both keys; tokens signed with the previous
   key keep validating until they expire.
5. After the overlap window (e.g. one access-token TTL), unset
   `BOND_MCPS_AS_PREVIOUS_KEY_PEM` and redeploy.

### Database

The same `tokens.db` (SQLite locally, Aurora Postgres in deployment)
hosts:
* Provider tokens (`provider_tokens`)
* MSAL caches (`msal_token_caches`)
* AS state (`oauth_clients`, `oauth_pending_auth`, `oauth_auth_codes`,
  `oauth_refresh_tokens`)
* Connect tickets (`connect_tickets`)

Migration `0002_oauth_authorization_server` adds the AS tables; run
`make migrate-db` after pulling this change.

### Health checks

Both processes expose `/healthz`:
* `curl http://localhost:8000/health` (proxy server, JSON `{"status":"ok"}`)
* `curl http://localhost:8001/healthz` (AS)
* `curl http://localhost:18002/healthz` (each MCP)

The MCP `/healthz` route is mounted unconditionally and does not depend on
JWT mode being on.

### Kubernetes deployment

The generic `deployment/helm/mcp-service` chart handles the AS and each MCP
via separate releases. A reference values file for the AS is at
`deployment/helm/mcp-service/values.auth-service.example.yaml` — copy it,
fill in placeholders (ACM cert ARN, Secrets Manager names, public host),
and `helm install bond-mcps-as deployment/helm/mcp-service -f my.yaml`.

For each MCP, set in your existing values file:

```yaml
jwt:
  enabled: true
  jwksUri: https://auth.mcps.example.com/.well-known/jwks.json
  issuer: https://auth.mcps.example.com
  # audience is auto-derived from publicUrl as `<publicUrl>/mcp` — the
  # canonical RFC 8707 resource URI Claude Code sends. Set explicitly only
  # if you need an additional friendly name (e.g. for non-Claude callers).
  asBaseUrl: https://auth.mcps.example.com
  publicUrl: https://github-mcp.mcps.example.com
```

That switches that MCP into Resource Server mode; no other changes needed
beyond the upstream IdP configuration on the AS.
