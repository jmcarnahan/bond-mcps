# bond-ai is a peer client of bond-mcps

## What changed

Earlier scaffolding routed bond-ai → bond-mcps requests through a custom
`X-Bond-Auth: Bearer <jwt>` header. That path was never wired up by any
caller and has been removed.

bond-mcps now stands up its own OAuth 2.1 Authorization Server
(`auth.auth_server`) and accepts only spec-compliant
`Authorization: Bearer <jwt>` on the standard header. bond-ai is just
another OAuth client — there is no special header or trust pathway.

## What this means for bond-ai

### bond-ai's user-facing web login is unchanged

bond-ai's own Google/Okta/Cognito login flow (in
`bondable/rest/routers/auth.py`) governs sign-in to the bond-ai web UI
and is decoupled from bond-mcps entirely. No bond-mcps deploy or upgrade
forces a change in bond-ai's web auth.

### If bond-ai needs to call bond-mcps server-to-server

Pick one of these patterns (out of scope for the current iteration —
implement when the requirement materialises):

1. **Confidential OAuth client + client_credentials grant.** bond-ai
   registers as a confidential client with the bond-mcps AS, obtains a
   token via `client_credentials`, and calls bond-mcps with it. This
   requires us to add `client_credentials` support to the AS — it
   currently only implements `authorization_code` and `refresh_token`
   (per OAuth 2.1's recommended public-client grants).

2. **Token exchange (RFC 8693).** bond-ai trades its end-user JWT for a
   bond-mcps-scoped JWT. Heavier; nice when bond-ai is acting "on behalf
   of" a logged-in user and you want auditable per-user calls into MCPs.

3. **Per-user user-agent flow.** bond-ai surfaces a "Connect to bond-mcps"
   button that drives Claude-Code-style PKCE OAuth and stashes the
   resulting JWT in bond-ai's per-user storage. Lightest server change in
   bond-mcps (no new grants); higher UX cost.

Whichever shape we pick, bond-ai stops being special.
