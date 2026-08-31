# Pre-deploy checklist

A step-by-step list of every thing you must do **before**, **during**, and
**after** `terraform apply` for a fresh multi-tenant (Claude Code) deployment.

The terraform module will catch most misconfigurations and stop with a
helpful error, but a few items live outside terraform's reach (provider
consoles, Cognito user pool admin) and have to be done manually.

---

## Before `terraform apply`

### 1. Pick + confirm AWS context
```bash
aws sts get-caller-identity
aws configure get region
```
Make sure you're in the right account and region. The deploy is regional.

### 2. Verify the existing VPC + Route53 zone
- `existing_vpc_id`: the VPC ID to deploy into (shared with bond-ai).
- `hosted_zone_id`: the Route53 hosted zone owning `base_domain`. Records
  for `auth.<base_domain>`, `ms-graph.<base_domain>`, etc will be created
  by the chart's ALB ingress.

### 3. Decide which MCP servers to deploy
In `environments/<env>.tfvars`, set `enabled = true` for each MCP block
in `services`. Disabled MCPs cost nothing.

### 4. Images build during apply
Terraform builds + pushes the repo-built images itself during `apply`
(`build-stages.tf`): each image's tag is a content hash of its sources, ECR
repos are created in the same apply, and the builds are ordered after repo
creation — no separate push step. Only foreign images (e.g. `sbel`, set via
a hand-pinned `image_tag`) must be pushed from their own repo before their
service is enabled. Requires a running Docker daemon with buildx
(cross-builds `linux/amd64` from Apple Silicon).

### 5. Pick Cognito (or Okta) coordinates
For Cognito reuse from bond-ai's user pool:
```bash
aws --region us-west-2 cognito-idp list-user-pools --max-results 20 \
  | jq -r '.UserPools[] | "\(.Id)\t\(.Name)"'
```
Note the pool ID — your `upstream_issuer` becomes
`https://cognito-idp.<region>.amazonaws.com/<pool-id>`.

The `upstream_client_id` is the app client you'll use. **Do NOT add the
deployed callback URL to Cognito yet** — wait until step 12 so you don't
fork the config across a half-deployed stack.

---

## During `terraform apply`

### 6. First apply
```bash
terraform init
terraform apply -var-file=environments/<env>.tfvars
```

Expect this **to fail** at the deploy-invariants precondition with a
message like:
```
${env}-encryption-key is not yet seeded.
Seed via: bond-mcps generate-key + aws secretsmanager put-secret-value
```

That's expected — the SM secrets are created with placeholders that you
must replace before the pods come up.

### 7. Seed the encryption key
```bash
# Generate a fresh AES-256 key
KEY=$(cd auth && poetry run bond-mcps generate-key)
aws secretsmanager put-secret-value \
  --secret-id <env>-encryption-key \
  --region us-west-2 \
  --secret-string "{\"BOND_MCPS_ENCRYPTION_KEY\":\"$KEY\"}"
```

### 8. Seed the AS credentials
```bash
# Generate an RSA-2048 signing key for the AS
openssl genrsa -out /tmp/as-key.pem 2048

jq -Rsn \
  --rawfile pem /tmp/as-key.pem \
  '{BOND_MCPS_AS_PRIVATE_KEY_PEM: $pem, BOND_MCPS_UPSTREAM_CLIENT_SECRET: ""}' \
  > /tmp/as-payload.json

aws secretsmanager put-secret-value \
  --secret-id <env>-as-credentials \
  --region us-west-2 \
  --secret-string file:///tmp/as-payload.json

rm /tmp/as-key.pem /tmp/as-payload.json
```

If your upstream OIDC client is confidential (Okta, Cognito with "Generate
client secret" enabled), put the secret in `BOND_MCPS_UPSTREAM_CLIENT_SECRET`.
For public clients (the bond-ai Cognito setup), leave it empty string.

### 9. Seed each provider's OAuth secret
For every enabled per-provider MCP, populate `${env}-${oauth_secret_name}`:

```bash
aws secretsmanager put-secret-value \
  --secret-id <env>-github-oauth \
  --region us-west-2 \
  --secret-string '{
    "GITHUB_CLIENT_ID": "...",
    "GITHUB_CLIENT_SECRET": "..."
  }'

aws secretsmanager put-secret-value \
  --secret-id <env>-microsoft-oauth \
  --region us-west-2 \
  --secret-string '{
    "MS_CLIENT_ID": "...",
    "MS_CLIENT_SECRET": "..."
  }'

aws secretsmanager put-secret-value \
  --secret-id <env>-atlassian-oauth \
  --region us-west-2 \
  --secret-string '{
    "ATLASSIAN_CLIENT_ID": "...",
    "ATLASSIAN_CLIENT_SECRET": "..."
  }'
```

The JSON keys map verbatim to env vars on the pod — they MUST match what
each MCP's auth.py expects.

### 10. Re-run `terraform apply`
With secrets seeded, the preconditions pass, helm releases proceed, ALB
provisions, ACM cert validates, pods come up.

```bash
terraform apply -var-file=environments/<env>.tfvars
```

This should reach `Apply complete!` in ~10-15 minutes (cold ALB +
Aurora startup dominates).

---

## After apply

### 11. Add the AS callback URL to Cognito (or Okta)
The AS's upstream callback is `${as_base_url}/oauth/upstream/callback`
where `as_base_url` = `https://auth.<base_domain>` by default.

**Cognito console:** *User pools → \<pool\> → App integration → App client
→ Edit hosted UI → Allowed callback URLs.* Add the AS URL. Don't remove
the existing entries (the laptop dev callback may also be there).

**Okta console:** *Applications → \<app\> → General → Sign-in redirect
URIs.* Add the AS URL.

### 12. Add per-provider callback URLs
Each MCP's connect flow is at `${mcp_url}/connect/<provider>/callback`.

- **GitHub** (one callback URL per OAuth app — you may need a separate
  app for deployed bond-mcps): set callback to
  `https://github.<base_domain>/connect/github/callback`.
- **Microsoft Azure AD app:** *App registrations → \<app\> →
  Authentication → Redirect URIs.* Add
  `https://ms-graph.<base_domain>/connect/microsoft/callback`.
- **Atlassian Developer Console:** *OAuth 2.0 → \<integration\> →
  Authorization → Callback URLs.* Add
  `https://atlassian.<base_domain>/connect/atlassian/callback`.

### 13. Smoke test against the deployed AS + MCPs

There's a ready-made smoke script. Run it from any workstation with Python
3.10+ and httpx:

```bash
pip install httpx
python scripts/smoke-deployed.py \
  --as-url   https://auth.<base_domain> \
  --mcp-url  https://microsoft.<base_domain>/mcp \
  --mcp-url  https://atlassian.<base_domain>/mcp \
  --mcp-url  https://github.<base_domain>/mcp
```

It checks all of these (exit code 0 on full pass):
- AS `/healthz` reachable; `/.well-known/oauth-authorization-server` is
  RFC 8414-shaped; issuer matches the requested URL; both
  `authorization_code` and `refresh_token` grants advertised
- JWKS returns at least one RS256 key with a kid
- DCR happy path produces a 201 with `client_id_issued_at` and
  `client_secret_expires_at: 0`
- DCR fail-closed: non-loopback HTTPS rejected when `BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS`
  isn't set (security guard)
- Per MCP: `/healthz`, protected-resource-metadata document points back at
  the AS, `POST /mcp` without Bearer returns 401 + `WWW-Authenticate`
  with `resource_metadata="..."`

If anything fails the script prints the failing check + a "next steps for
common failure modes" hint block.

Or, the manual curl equivalent for ad-hoc poking:

```bash
curl -s https://auth.<base_domain>/.well-known/oauth-authorization-server | jq .
curl -s https://auth.<base_domain>/.well-known/jwks.json | jq '.keys[0].kid'
curl -s -o /dev/null -D - https://microsoft.<base_domain>/mcp \
  -X POST -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
  | grep -i www-authenticate
```

### 14. Register with Claude Code from a workstation
```bash
claude mcp add --transport http ms-graph  https://ms-graph.<base_domain>/mcp
claude mcp add --transport http atlassian https://atlassian.<base_domain>/mcp
claude mcp list
```

In Claude Code: `/mcp` → pick an MCP → complete the Cognito sign-in → ask
a question that requires the upstream provider → click the
`/connect/<provider>` URL surfaced by the missing_provider_connection
error → done.

---

## Operational tasks

### Periodic pruning of OAuth artifacts
DCR clients accumulate as users churn through Claude Code sessions
(known Claude Code bug #43000 — each restart can mint a fresh DCR
client). Schedule the cleanup CLI on a daily cron:

```bash
# Inside the AS pod (or any pod with the auth image)
bond-mcps prune-oauth --client-idle-days 30 --revoked-grace-days 7
```

Or wire it into k8s as a CronJob using the same auth image.

### Revoking a user's sessions (emergency)
If a workstation is lost/stolen, invalidate all refresh tokens for that
user_key:

```bash
bond-mcps revoke-tokens --user-key=<cognito-sub>
```

The user's bond-mcps JWT remains valid until its TTL (24h default), but
no new refresh is possible.

### Rotating the AS signing key
1. Generate a fresh PEM.
2. Set `BOND_MCPS_AS_PREVIOUS_KEY_PEM` to the **current** key (in
   `<env>-as-credentials` SM secret).
3. Set `BOND_MCPS_AS_PRIVATE_KEY_PEM` to the **new** key.
4. Roll the AS deployment. JWKS now publishes both `kid`s; tokens signed
   with the previous key still validate.
5. After the overlap window (at least one `ACCESS_TOKEN_TTL_SECONDS`),
   remove `BOND_MCPS_AS_PREVIOUS_KEY_PEM`.

### Lengthening the AS-issued JWT TTL
Edit `jwt_verification.access_token_ttl_seconds` in your tfvars and
re-apply. Restarts the AS pod with the new value. Old tokens remain
valid until their original `exp`.

---

## What can go wrong (and how to spot it)

| Symptom | Cause | Fix |
|---|---|---|
| Pods stuck in `ImagePullBackOff` | ECR repos exist but no image pushed | Build + push the image, then `kubectl rollout restart` |
| ALB target group `unhealthy` | `/healthz` not returning 200 | `kubectl logs <pod>` — usually a missing env var |
| Claude Code shows `Failed to connect` after `claude mcp add` | AS URL not reachable / wrong TLS / DNS not propagated | `curl -v https://auth.<base_domain>/healthz` |
| `audience mismatch` in MCP log | Cognito callback URL not added | Re-check step 11 |
| `Upstream token exchange failed: HTTP 401` in AS log | `BOND_MCPS_UPSTREAM_CLIENT_SECRET` wrong/missing for a confidential upstream client | Put-secret-value the right secret |
| `RuntimeError: BOND_MCPS_AS_BASE_URL must be set` | AS pod started before SM secret seeded | Confirm `<env>-as-credentials` is seeded, then `kubectl rollout restart` |
