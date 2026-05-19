# Adding an upstream IdP plugin

The bond-mcps Authorization Server currently supports two upstream OIDC
providers: **Cognito** and **Okta**. Adding a third (e.g. Azure AD, Auth0,
Google directly) is straightforward because both shipping plugins are
backed by the same generic `OIDCUpstreamIdP` class.

## What "upstream IdP" means here

The bond-mcps AS is itself an OAuth/OIDC client to one configured IdP.
The AS handles its own end of OAuth 2.1 (PKCE, DCR, JWKS, audience-
bound JWTs) and delegates user authentication ("who is signing in?") to
the upstream. Each deployment selects one upstream via
`BOND_MCPS_UPSTREAM_IDP`.

The upstream contract is:

* Speak OIDC discovery at `<issuer>/.well-known/openid-configuration`.
* Issue authorization codes via the `authorization_endpoint`.
* Exchange codes for an `id_token` (plus an `access_token`) at the
  `token_endpoint`.
* Include at minimum `sub` and `email` claims in the `id_token`.

That's enough for the AS to ferry the user identity into its own JWT.

## Adding a new IdP

If the upstream is a vanilla OIDC provider, no code changes are required
beyond registering its name in the `SUPPORTED_IDPS` set:

```python
# auth/auth/auth_server/upstream.py
SUPPORTED_IDPS = {"cognito", "okta", "azure_ad"}  # new entry
```

Then deploy with:

```
BOND_MCPS_UPSTREAM_IDP=azure_ad
BOND_MCPS_UPSTREAM_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
BOND_MCPS_UPSTREAM_CLIENT_ID=...
BOND_MCPS_UPSTREAM_CLIENT_SECRET=...
BOND_MCPS_UPSTREAM_REDIRECT_URI=https://auth.example.com/oauth/upstream/callback
```

That's it — the existing `OIDCUpstreamIdP` handles discovery, PKCE,
id_token decode, and email-domain gating.

## When you need a custom plugin

If the upstream isn't quite OIDC (custom auth scheme, non-standard
discovery, additional claims to extract), implement the
`UpstreamIdP` protocol:

```python
class UpstreamIdP(Protocol):
    def authorize_url(self, *, state, code_challenge, code_challenge_method="S256") -> str: ...
    def exchange_code(self, *, code, code_verifier) -> UpstreamUserInfo: ...
```

Then teach `get_upstream_idp()` (in `upstream.py`) to construct your
plugin when its name appears in `BOND_MCPS_UPSTREAM_IDP`. Keep all
discovery / token-endpoint chatter inside the plugin — the rest of the
AS just calls `authorize_url()` and `exchange_code()`.

## Email-domain gating

Set `BOND_MCPS_UPSTREAM_ALLOWED_DOMAINS=acme.com,bond.example` to refuse
sign-ins from users whose email isn't in the list. The check runs against
the `email` claim of the upstream id_token, after PKCE verification but
before any auth code is issued to the MCP client.

## Why we skipped Google

Google's OAuth/OIDC behaves like any other upstream, but it didn't make
the cut for the first iteration because:

* It has no MFA / org policy enforcement of its own (operators want
  Cognito or Okta in front of it anyway).
* It would invite questions about whether sign-in maps to Google Workspace
  accounts vs personal accounts.

Adding it later is a one-line `SUPPORTED_IDPS` change plus an env config
example.
