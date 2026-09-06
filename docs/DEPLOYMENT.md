# bond-mcps Deployment Guide

End-to-end deployment of a forked bond-mcps stack to AWS EKS. Written for Claude
Code to follow step-by-step in a fresh fork — every command is copy-paste,
every step has a verification check.

## Architecture (1 page)

```
                  ┌───────────────────────────────────────────────┐
                  │       Upstream OIDC IdP (Cognito or Okta)     │
                  │       — sign-in for humans, issues id_token   │
                  └───────────────────────┬───────────────────────┘
                                          │ /oauth/upstream/callback
                                          ▼
  Claude Code  ──→  https://auth.<base>/oauth/authorize  ──→  AS pod  ──→
                                                                   │
                              JWT (per-resource aud)               │  RSA-256 JWKS @
                                  ◀────────────────────────────────┘  /.well-known/jwks.json
                              + refresh_token

  Claude Code  ──→  https://<mcp>.<base>/mcp  (Bearer <JWT>)
                          │
                          ├─ FastMCP RemoteAuthProvider validates via JWKS
                          ├─ user_key = sub claim
                          └─ tool call ──→ provider API (Graph / Atlassian / GitHub / …)
                                              │
                                              │ if no provider token yet:
                                              └→ missing_provider_connection
                                                  + /connect/<provider>?ticket=…
                                                       │
                                                       ▼
                                         provider OAuth → provider_tokens row in Aurora
```

- 1 Authorization Server + N MCPs share a single ALB (IngressGroup `bond-mcps`).
- Aurora Postgres Serverless v2 (`bond_mcps` DB) stores encrypted upstream tokens.
- AWS Secrets Manager + ExternalSecrets Operator inject runtime secrets.
- All ingress HTTPS via one wildcard ACM cert for `*.<base_domain>`.

## TL;DR (9 steps)

```
1.  Fork repo, install tools, get AWS context.
2.  Create Cognito user pool + PKCE-only app client (or use Okta).
3.  Copy environments/dev.tfvars.example → environments/<env>.tfvars; edit.
4.  make deploy-plan && make deploy
    → fails at encryption_key_seeded precondition (expected, before any image build).
5.  Seed AWS Secrets Manager (encryption-key, as-credentials, per-provider OAuth secrets).
6.  Add https://auth.<base>/oauth/upstream/callback to upstream IdP client.
7.  make deploy-plan && make deploy   # second pass: builds + pushes images, completes.
8.  make smoke                        # all green.
9.  Per-provider OAuth app callback URLs, then claude mcp add + /mcp.
```

## 1. Prerequisites

### Tools (local workstation)
- terraform `>= 1.6`
- helm `>= 3.13`
- kubectl `>= 1.30`
- AWS CLI v2 with credentials for the target account
- docker with buildx (for cross-arch builds; Docker Desktop or `docker buildx install`)
- poetry `>= 1.7` (for `make dev`, image build prep, smoke-test deps)
- python `>= 3.10` (smoke test uses httpx)
- jq, dig (DNS verification)

### AWS account
| Resource | Why |
|---|---|
| EKS create/delete | Cluster, node group, addons |
| VPC describe + tag | Reuses existing VPC; no VPC create needed |
| Route53 hosted zone (full edit) | Wildcard ACM cert validation, per-service A records |
| ACM cert (regional, us-west-2 or your region) | Wildcard `*.<base_domain>` |
| RDS Aurora full | Cluster + 1 serverless v2 instance |
| Secrets Manager + KMS | Encryption keys, OAuth creds |
| ECR full | One repo per service |
| IAM (create roles/policies) | IRSA for ALB controller, ESO, node group |

The deployer needs `AdministratorAccess` or an equivalent custom policy
covering all of the above.

### Network prerequisites (existing VPC)
| Need | Why |
|---|---|
| At least 2 private subnets in different AZs with NAT egress | Aurora HA + EKS nodes pulling images |
| At least 2 public subnets in different AZs | Shared ALB ENIs |
| Route table: 0.0.0.0/0 → NAT for private subnets | Pods need internet for ECR/AWS APIs |
| (Optional) `kubernetes.io/role/elb` tag on public subnets | NOT required — we pass `public_subnet_ids` explicitly |

### Upstream IdP

You need ONE OIDC IdP. Cognito recommended; Okta also supported.

**Cognito (recommended for AWS-native deployments):**
1. Create user pool: User Pools → Create. Defaults are fine.
2. Add hosted UI domain: User Pool → App Integration → Domain → Set up.
3. Create app client:
   - Type: **Public client** (PKCE-only, no secret)
   - Allowed OAuth flows: **Authorization code grant**
   - Allowed OAuth scopes: **openid**, **email** (add **profile** if available)
   - Don't add callback URLs yet (step 7).
4. Capture: pool ID (`us-west-2_xxx`), client ID, region.

**Okta:**
1. Applications → Create App Integration → OIDC – Web (or SPA for public).
2. Sign-in redirect URIs: leave blank for now.
3. Grant types: Authorization Code + Refresh Token + PKCE.
4. Capture: issuer (`https://<org>.okta.com/oauth2/default`), client ID,
   client secret (Web app only).

### Domain
- Buy or transfer a domain into Route53.
- Identify your base subdomain (e.g. `mcps.example.com`).
- Hosted zone ID for the parent zone (`example.com`) is what terraform needs;
  records under `mcps.example.com` are created in the parent zone unless
  delegated.

## 2. Local validation

Catches code bugs before you spend AWS dollars. Skip on a clean upstream fork
where nothing has been modified.

### 2a. `make dev` — proxy-mode local stack
```bash
cd <fork-root>
make migrate-db
make dev          # boots auth proxy + 4 MCPs on localhost
make status       # all [up]
curl -s http://localhost:18001/healthz   # 200 ok
make stop
```

### 2b. `make dev-multitenant` — JWT mode (closer to deployed)
```bash
make dev-multitenant
make status-mt    # all 6 services [up] including the AS on :18001

# AS metadata
curl -s http://localhost:18001/.well-known/oauth-authorization-server | jq .issuer
# Each MCP returns 401 + WWW-Authenticate
curl -i http://localhost:18101/mcp -X POST -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
```

Then in a separate terminal:
```bash
claude mcp add --transport http microsoft-local http://localhost:18101/mcp
claude
# /mcp → microsoft-local → authenticate (browser opens to localhost AS)
```

If the local OAuth dance works, the deployed one will work modulo
infrastructure config. Tear down with `make stop-mt`.

## 3. Configure `environments/<env>.tfvars`

```bash
cd deployment/terraform-existing-vpc
cp environments/example.tfvars environments/<env>.tfvars
# Edit every <PLACEHOLDER>. The annotated template inline below mirrors
# example.tfvars and includes the gotchas as comments.
```

Template:

```hcl
# --- Identity / region -----------------------------------------------------
environment = "dev"                  # ALBs are tagged with this; must be unique per stack

# --- Existing VPC ---------------------------------------------------------
existing_vpc_id    = "vpc-<your-vpc>"
private_subnet_ids = ["subnet-<az-a>", "subnet-<az-b>"]   # 2+ AZs, NAT egress
public_subnet_ids  = ["subnet-<pub-a>", "subnet-<pub-b>"] # 2+ AZs, ALB lives here

# --- DNS + TLS ------------------------------------------------------------
base_domain    = "mcps.<your-domain>"
hosted_zone_id = "Z<your-zone-id>"

# --- Dev knobs (loosen in prod) ------------------------------------------
aurora_deletion_protection   = false
ecr_force_delete             = true   # let `terraform destroy` clean images
secrets_recovery_window_days = 0      # 0 = immediate delete (dev); prod: 7-30
aurora_min_capacity          = 0.5    # ACU; default 1.0

# --- JWT mode + upstream IdP ---------------------------------------------
jwt_verification = {
  enabled               = true
  upstream_idp          = "cognito"            # or "okta"
  upstream_issuer       = "https://cognito-idp.<region>.amazonaws.com/<pool-id>"
  upstream_client_id    = "<app-client-id>"
  upstream_redirect_uri = "https://auth.<base_domain>/oauth/upstream/callback"
  # Cognito public clients support only the scopes you enabled on the client.
  # Minimum: "openid email". Add "profile" if available on the client.
  upstream_scopes = "openid email"
  # Empty = loopback-only DCR (safe default). To allow https reply URLs from
  # an org workstation pool, put the hostname CSV here.
  as_allowed_redirect_hosts = ""
}

# --- Services ------------------------------------------------------------
services = {
  # Authorization Server: required when jwt_verification.enabled. Exactly one.
  auth_server = {
    enabled         = true
    image_repo_name = "bond-mcps-auth"
    build           = "auth"            # built by terraform apply; tag = content hash
    hostname_prefix = "auth"            # → auth.<base_domain>
    container_port  = 8001
    replicas        = 1                 # AS is stateless DB-backed; can scale, doesn't need to
    is_auth_server  = true
    runs_migrations = true              # runs alembic + bond-mcps doctor as initContainers
    command         = ["python", "-m", "auth.auth_server", "--host", "0.0.0.0", "--port", "8001"]
    health          = { type = "http", path = "/healthz" }
  }

  microsoft = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-microsoft"
    build             = "microsoft"
    # IMPORTANT: do NOT use "microsoft" as the hostname prefix. Entra ID
    # rejects reply URLs whose hostname contains Microsoft-trademark terms.
    hostname_prefix   = "ms-graph"
    replicas          = 2
    oauth_secret_name = "microsoft-oauth"
    extra_env = {
      # consumers = personal MSA; switch to your tenant GUID for org accounts.
      MS_TENANT_ID = "consumers"
      # Hide mail from senders outside these domains (unset = off). List every
      # domain the org sends from, including <tenant>.onmicrosoft.com.
      MS_MAIL_ALLOWED_SENDER_DOMAINS = "yourcompany.com,yourcompany.onmicrosoft.com"
      # Scopes each sign-in requests, space-separated. Unset = the consented
      # default in ms_graph/local_auth.py. Widen only AFTER the admin has
      # granted the extra scopes (README: "Directory search rollout").
      # MS_SCOPES = "Mail.Read Mail.ReadWrite Mail.Send MailboxSettings.Read User.Read Files.Read.All Chat.ReadWrite User.ReadBasic.All"
    }
    health = { type = "http", path = "/healthz" }
  }

  atlassian = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-atlassian"
    build             = "atlassian"
    hostname_prefix   = "atlassian"
    replicas          = 2
    oauth_secret_name = "atlassian-oauth"
    health            = { type = "http", path = "/healthz" }
  }

  github = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-github"
    build             = "github"
    hostname_prefix   = "github"
    replicas          = 2
    oauth_secret_name = "github-oauth"
    health            = { type = "http", path = "/healthz" }
  }

  databricks = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-databricks"
    build             = "databricks"
    hostname_prefix   = "databricks"
    replicas          = 2
    oauth_secret_name = "databricks-oauth"
    extra_env = {
      DATABRICKS_HOST      = "https://<workspace>.cloud.databricks.com"
      DATABRICKS_HTTP_PATH = "/sql/1.0/warehouses/<warehouse-id>"
    }
    health = { type = "http", path = "/healthz" }
  }
}
```

**Note on `MS_MAIL_ALLOWED_SENDER_DOMAINS`:** this variable is the
external-sender mail policy's toggle and its allowlist at once. Unset or blank
means off and every mail tool behaves as before; a comma-separated list of
domains means mail whose Exchange `from` (or `sender`) is outside those domains
is hidden from every mail surface. It is per-deployment, so a staging
environment can run with it off while production runs with it on. A malformed
value — a bare word, a `*`, an empty label — stops the pod at boot: readiness
never passes and the log names the bad entry, which is deliberate, because
serving mail unfiltered would be the worse failure. At startup the pod logs
`Mail sender policy: on (N allowed domain(s))` or
`Mail sender policy: off (MS_MAIL_ALLOWED_SENDER_DOMAINS unset)`. See the "Mail
sender policy" section of `mcps/microsoft/README.md` for the rule, the coverage
list, and the rollout checklist.

**Note on `build` vs `image_tag`:** every service sets exactly one of the two
(terraform validation enforces it). Services built from this repo set
`build = "<key>"` — one of `auth`, `microsoft`, `atlassian`, `github`,
`databricks` — and their tag is a content hash computed at plan time; there is
no tag to hand-edit. A foreign image built in another repo (e.g. `sbel`) keeps
a hand-pinned `image_tag`, because terraform cannot build it. See §6.

**Note on `.gitignore`:** the repo's `.gitignore` has an exception that *keeps*
`environments/*.tfvars` checked in. This is intentional — tfvars hold
identifiers (pool ID, subnet IDs, hosted zone ID), NOT secrets. Real secrets
live in AWS Secrets Manager. If your org policy forbids tracking these
identifiers, remove the `!deployment/terraform-existing-vpc/environments/*.tfvars`
line from `.gitignore`.

## 4. First terraform apply (expect to fail)

```bash
cd <fork-root>
export AWS_REGION=us-west-2          # MUST match your tfvars region
aws sts get-caller-identity          # confirm right account
make deploy-plan TF_ENV=<env>        # preflight + guards, then terraform plan
# Review: should be ~100 resources for a fresh deploy
make deploy TF_ENV=<env>             # applies exactly the plan you just read
```

`TF_ENV` defaults to `dev`, so plain `make deploy-plan && make deploy` is the
usual invocation. `deploy-plan` runs `make deploy-check` first: tooling
(terraform, aws, jq, docker + buildx, a running docker daemon), the tfvars
file, AWS credentials, and the git guards — clean tree, on `main`, HEAD equal
to `origin/main`. **The working tree is the input**: image tags are hashed from
the files on disk, not from a git ref, so a dirty tree would ship uncommitted
code. `DEPLOY_UNSAFE=1` skips the three git checks (never the tooling ones)
when you deliberately want to deploy something other than merged `main`.

**Expected failure** (~15-20 min in):
```
Error: Resource precondition failed
  …encryption_key is not yet seeded (status: missing or placeholder).
```

The image builds depend on this precondition, so the apply fails *before*
spending 15-30 min on cross-arch builds it could never deploy.

Infrastructure exists. Now seed.

## 5. Seed AWS Secrets Manager

The terraform module creates SM secrets with placeholder values. You must
populate real values **before** the helm releases can start successfully.

### 5a. Encryption key (gates apply via precondition)
```bash
KEY=$(cd auth && poetry run bond-mcps generate-key)
aws --region us-west-2 secretsmanager put-secret-value \
  --secret-id <env>-encryption-key \
  --secret-string "{\"BOND_MCPS_ENCRYPTION_KEY\":\"$KEY\"}"
```

### 5b. AS credentials (RSA signing key + upstream client secret)
```bash
openssl genrsa -out /tmp/as-key.pem 2048
# UPSTREAM_CLIENT_SECRET: empty string for Cognito PKCE-only; real secret for Okta confidential clients.
jq -Rsn --rawfile pem /tmp/as-key.pem \
  '{BOND_MCPS_AS_PRIVATE_KEY_PEM: $pem, BOND_MCPS_UPSTREAM_CLIENT_SECRET: ""}' \
  > /tmp/as-payload.json
aws --region us-west-2 secretsmanager put-secret-value \
  --secret-id <env>-as-credentials --secret-string file:///tmp/as-payload.json
rm /tmp/as-key.pem /tmp/as-payload.json
```

### 5c. Per-provider OAuth secrets
JSON keys must match the env vars each MCP's `auth.py` reads.

```bash
# Microsoft (Azure AD or personal MSA app)
aws --region us-west-2 secretsmanager put-secret-value \
  --secret-id <env>-microsoft-oauth \
  --secret-string '{"MS_CLIENT_ID":"<id>","MS_CLIENT_SECRET":"<secret>"}'

# Atlassian (Developer Console OAuth 2.0 integration)
aws --region us-west-2 secretsmanager put-secret-value \
  --secret-id <env>-atlassian-oauth \
  --secret-string '{"ATLASSIAN_CLIENT_ID":"<id>","ATLASSIAN_CLIENT_SECRET":"<secret>"}'

# GitHub (NEW OAuth app — one callback URL per app; can't reuse a localhost one)
aws --region us-west-2 secretsmanager put-secret-value \
  --secret-id <env>-github-oauth \
  --secret-string '{"GITHUB_CLIENT_ID":"<id>","GITHUB_CLIENT_SECRET":"<secret>"}'

# Databricks (PAT mode — replace placeholders later if you want real DBX)
aws --region us-west-2 secretsmanager put-secret-value \
  --secret-id <env>-databricks-oauth \
  --secret-string '{"DATABRICKS_ACCESS_TOKEN":"<pat-or-placeholder>"}'
```

**Note:** if you disable any MCP service via `enabled = false`, the
corresponding `<env>-<name>-oauth` SM secret is not created — skip its put.

## 6. Images (built by terraform)

There is no build step to run. `terraform apply` builds and pushes every
repo-built image itself (`deployment/terraform-existing-vpc/build-stages.tf`):

- Each image's tag is a 12-char md5 content hash of exactly the files its
  Dockerfile `COPY`s — plus, for the MCP images, the vendored auth package
  (`auth/auth/**/*.py`, `**/*.mako`, `auth/pyproject.toml`, `auth/poetry.lock`)
  that gets staged into `_shared_auth_pkg/` at build time.
- Changed code ⇒ new tag ⇒ terraform builds (`docker buildx`,
  `--platform linux/amd64`, builder `bond-mcps-builder`), pushes, and the helm
  release rolls. Unchanged code ⇒ same tag ⇒ no-op.
- If the tag is already in ECR, the build is skipped:
  `==> [microsoft] a1b2c3d4e5f6 already exists in ECR — skipping build`.

**Cross-arch build on Apple Silicon takes 15-30 min the first time.** Buildx
cache speeds up subsequent runs to <5 min.

What the current tree hashes to (also visible in the plan output):
```bash
terraform -chdir=deployment/terraform-existing-vpc output -json built_images | jq .
terraform -chdir=deployment/terraform-existing-vpc output -json deployed_image_tags | jq .
```
`built_images` is the images terraform builds for enabled services;
`deployed_image_tags` is the tag each service actually deploys (content hash
for repo-built images, the hand-pinned `image_tag` for foreign ones like
`sbel`).

Verify the tags landed in ECR:
```bash
TAGS=$(terraform -chdir=deployment/terraform-existing-vpc output -json built_images)
for pair in bond-mcps-auth:auth bond-mcps-mcp-microsoft:microsoft \
            bond-mcps-mcp-atlassian:atlassian bond-mcps-mcp-github:github \
            bond-mcps-mcp-databricks:databricks; do
  repo=${pair%%:*}; key=${pair##*:}
  tag=$(printf '%s' "$TAGS" | jq -r --arg k "$key" '.[$k] // empty')
  [ -n "$tag" ] || { printf "%-30s (not enabled)\n" "$repo"; continue; }
  printf "%-30s %s " "$repo" "$tag"
  aws --region us-west-2 ecr describe-images --repository-name "$repo" \
    --image-ids imageTag="$tag" --query 'imageDetails[0].imageDigest' --output text
done
```

Every enabled service should print a `sha256:` digest.

## 7. Add upstream IdP callback URL

The AS uses one callback URL at the upstream IdP. Add it now (before the
second apply, so the first OAuth attempt works immediately).

**Cognito:** User Pools → \<your pool\> → App integration → Your app client →
Edit hosted UI settings → Allowed callback URLs → add:
```
https://auth.<base_domain>/oauth/upstream/callback
```
Don't remove existing localhost entries.

**Okta:** Applications → \<your app\> → General → Sign-in redirect URIs → add
the same URL.

## 8. Second terraform apply

```bash
cd <fork-root>
make deploy-plan TF_ENV=<env>
make deploy TF_ENV=<env>
```

This time it completes. On a fresh fork this is the apply that does the cold
cross-arch image builds (15-30 min, §6) before the ~5-10 min of infrastructure
work: the 4 helm releases roll out, ALB provisions, ACM cert validates,
Route53 A records point to the ALB.

If you see `helm_release` failures, see Troubleshooting → "Helm releases stuck".

## 9. Smoke test

```bash
sudo killall -HUP mDNSResponder    # macOS only — flush negative DNS cache
                                    # (DNS records were just created; the OS
                                    # may have cached "doesn't exist")

make smoke                          # reads terraform's service_urls output
```

`make smoke` passes the AS via `--as-url` and every other enabled service via
`--mcp-url`, skipping `auth_server` (already covered as the AS) and `sbel`
(foreign data-only image, no PRM contract). Override the skip list with
`SMOKE_SKIP="auth_server sbel"`.

Equivalent manual invocation:
```bash
python scripts/smoke-deployed.py \
  --as-url   https://auth.<base_domain> \
  --mcp-url  https://ms-graph.<base_domain>/mcp \
  --mcp-url  https://atlassian.<base_domain>/mcp \
  --mcp-url  https://github.<base_domain>/mcp \
  --mcp-url  https://databricks.<base_domain>/mcp
```

Pass criteria (exit 0):
- AS `/healthz` 200, issuer matches, both grant types advertised
- JWKS has RS256 key with kid
- DCR happy path 201; non-loopback HTTPS rejected without `as_allowed_redirect_hosts`
- Each MCP: `/healthz` 200, PRM resource = canonical URI, 401 + Bearer +
  resource_metadata on unauthenticated POST

Any failure → check pod logs:
```bash
aws eks update-kubeconfig --name <env>-eks --region us-west-2
kubectl get pods -n bond-mcps
kubectl logs -n bond-mcps deploy/auth-server --tail=50
kubectl logs -n bond-mcps deploy/ms-graph    --tail=50
```

## 10. Per-provider OAuth app callback URLs

Each provider needs the deployed connect URL added so users can complete
the per-provider OAuth dance after upstream sign-in.

| Provider | Where to add | URL to add |
|---|---|---|
| Microsoft | Azure portal → App registrations → \<app\> → Authentication → Redirect URIs → Web | `https://ms-graph.<base_domain>/connect/microsoft/callback` |
| Atlassian | developer.atlassian.com → Console → \<integration\> → Authorization → Callback URL | `https://atlassian.<base_domain>/connect/atlassian/callback` |
| GitHub | github.com/settings/applications/new (one-callback-per-app limit forces a NEW app per environment) | `https://github.<base_domain>/connect/github/callback` |
| Databricks | n/a — PAT mode | seed PAT in `<env>-databricks-oauth` |

**Microsoft caveat:** Azure rejects hostnames containing the word "microsoft".
The tfvars template uses `hostname_prefix = "ms-graph"` for exactly this
reason — don't change it.

## 11. Register with Claude Code

```bash
claude mcp add --transport http microsoft-deployed  https://ms-graph.<base_domain>/mcp
claude mcp add --transport http atlassian-deployed  https://atlassian.<base_domain>/mcp
claude mcp add --transport http github-deployed     https://github.<base_domain>/mcp
claude mcp add --transport http databricks-deployed https://databricks.<base_domain>/mcp
claude mcp list
```

Then in Claude Code:
1. `/mcp` → pick one → "Authenticate"
2. Browser opens → upstream IdP sign-in → redirect back → Claude Code stores the bond-mcps JWT (24h TTL by default)
3. Ask a tool to do something (e.g. "list my recent emails")
4. First call returns `missing_provider_connection` with a connect URL —
   open it, complete the provider's OAuth, then retry.
5. Subsequent calls use the stored provider token (auto-refreshed server-side).

One Cognito sign-in covers a working day across every MCP. Re-prompt happens
after 24h JWT expiry (refresh_token grant is implemented but Claude Code
2.1.x has bugs that mostly bypass it — a restart of `claude` usually
re-authenticates without browser prompt as long as the JWT is unexpired).

## Operations

### Image rebuild + redeploy
There is no tag to bump — the tag is derived from the content of the files the
Dockerfile copies (§6), so changed code is already a changed tag. When you
change MCP or AS code:
1. Merge the change to `main` and pull, so the working tree is exactly
   `origin/main` (the guards in `make deploy-check` insist on it — the images
   are built from the tree, not from a git ref).
2. `make deploy-plan` — read the plan. Expected for a code release:
   `null_resource.build[...]` replaced for each changed image, and
   `module.service[...].helm_release.service` updated in place with the new
   tag. Aurora, EKS, KMS, Secrets Manager or ACM appearing is not.
3. `make deploy` — applies exactly that saved plan (it refuses if HEAD moved
   since the plan was made) and deletes it afterwards.
4. `make deploy-status` — terraform's `deployed_image_tags` next to what the
   cluster is actually running.

Unchanged images are skipped at apply time with
`already exists in ECR — skipping build`, so redeploying costs nothing for the
services you didn't touch. The chart's `image.pullPolicy=IfNotPresent` is safe
because a new content hash is always a new tag.

**`force_rebuild` (escape hatch).** The hash covers files, not the
`python:3.12-slim` base image and not apt/PyPI resolution during the build. To
force fresh tags for everything the hash cannot see, bump the token in tfvars:
```hcl
force_rebuild = "2026-09-01-cve"
```
Then `make deploy-plan && make deploy`. A new token mints new tags for every
repo-built image, which can never hit the skip-if-exists path.

### Secret rotation
- **Encryption key** (master key for token encryption-at-rest):
  ```bash
  KEY=$(cd auth && poetry run bond-mcps generate-key)
  aws --region us-west-2 secretsmanager put-secret-value \
    --secret-id <env>-encryption-key \
    --secret-string "{\"BOND_MCPS_ENCRYPTION_KEY\":\"$KEY\"}"
  kubectl rollout restart deploy -n bond-mcps   # pods pick up new key on restart
  ```
  Existing rows in `provider_tokens` remain decryptable as long as the OLD
  key value is also configured (re-encryption migration TBD).

- **AS signing key** (overlap window for JWKS):
  1. Generate new RSA PEM.
  2. Put new PEM in `BOND_MCPS_AS_PRIVATE_KEY_PEM`, OLD PEM in
     `BOND_MCPS_AS_PREVIOUS_KEY_PEM` (in `<env>-as-credentials`).
  3. `kubectl rollout restart deploy/auth-server -n bond-mcps`
  4. JWKS now publishes both `kid`s. Tokens signed with the old key still
     verify until they expire.
  5. After `access_token_ttl_seconds` of grace, remove
     `BOND_MCPS_AS_PREVIOUS_KEY_PEM` and roll again.

- **Per-provider OAuth secret** (e.g. you rotate the Microsoft app secret):
  ```bash
  aws --region us-west-2 secretsmanager put-secret-value \
    --secret-id <env>-microsoft-oauth \
    --secret-string '{"MS_CLIENT_ID":"<id>","MS_CLIENT_SECRET":"<new>"}'
  ```
  ESO refreshes every 5 min. Or force: `kubectl rollout restart deploy/ms-graph -n bond-mcps`.

### Periodic cleanup (DCR clients, expired codes/tickets)
Claude Code 2.1.x mints a fresh DCR client on every restart (bug #43000).
Schedule the prune CLI daily:
```bash
kubectl exec -n bond-mcps deploy/auth-server -- \
  bond-mcps prune-oauth --client-idle-days 30 --revoked-grace-days 7
```
Or wire it as a CronJob using the same auth image.

### Revoke a user's sessions (emergency)
```bash
kubectl exec -n bond-mcps deploy/auth-server -- \
  bond-mcps revoke-tokens --user-key=<upstream-sub>
```
Existing JWTs remain valid until expiry (24h default); no new refresh.

### Tear down an environment
```bash
cd deployment/terraform-existing-vpc
terraform destroy -var-file=environments/<env>.tfvars
```
With `ecr_force_delete=true` and `secrets_recovery_window_days=0`, this
cleans up completely. Production should leave both defaults to retain
disaster-recovery options.

## Troubleshooting matrix

| Symptom | Cause | Fix |
|---|---|---|
| `terraform plan` fails: validation rule on `jwt_verification` | Missing `upstream_*` fields | All four of `upstream_idp`, `upstream_issuer`, `upstream_client_id`, `upstream_redirect_uri` required when enabled |
| `terraform apply` fails at `encryption_key_seeded` precondition | First apply — secrets unsealed | Seed per §5, re-apply |
| EKS node group: `NodeCreationFailure: Unhealthy nodes` | `vpc-cni` installed AFTER nodes; CNI absent → kubelet NotReady | Already mitigated: `eks.tf` sets `before_compute = true` on `vpc-cni` and `kube-proxy`. If this regresses, look for `before_compute` removal |
| ALB controller CrashLoopBackOff: `failed to fetch VPC ID from instance metadata` | IMDS blocked from pods (EKS default hop-limit=1) | Already mitigated: `eks-lb-controller.tf` passes `vpcId`/`region` explicitly. Confirm chart values include them |
| ESO install fails: `no endpoints available for service aws-load-balancer-webhook-service` | ALB controller webhook intercepts ESO Service creation before LB controller pod is ready | Already mitigated: `external-secrets.tf` has `depends_on = [helm_release.alb_controller]` |
| Service helm release: invalid release name (underscores) | Service key has `_` (e.g. `auth_server`) | Already mitigated: module sanitizes `_` → `-` in release name AND chart `nameOverride` |
| Pod CrashLoopBackOff: `ModuleNotFoundError: starlette` (or uvicorn / httpx / python-multipart) | AS image missing deps | Already mitigated in `auth/pyproject.toml`; the dep change rehashes the image, so `make deploy-plan && make deploy` rebuilds it |
| Pod ImagePullBackOff: `<tag>: not found` | ECR has no image at the tag the release wants | `make deploy-plan && make deploy` — the apply builds any tag ECR is missing. For a foreign image (`sbel`) push the pinned `image_tag` from its own repo |
| Pod still running old code after a deploy | Content hash unchanged — the edited files aren't `COPY`ed into the image, or the drift is invisible to the hash (base image, apt/PyPI) | Compare `terraform -chdir=deployment/terraform-existing-vpc output -json built_images` before/after; for invisible drift bump `force_rebuild` in tfvars and re-deploy |
| Pod CreateContainerConfigError: `secret "<svc>-jwt" not found` | Chart's envFrom referenced a JWT static-PEM secret that's not created in JWKS-URI mode | Already mitigated: `_helpers.tpl` gates on `jwt.publicKey.secretsManagerName` |
| ALB controller logs: `couldn't auto-discover subnets: 0 match VPC and tags: [kubernetes.io/role/elb]` | Shared VPC's public subnets aren't tagged | Already mitigated: `public_subnet_ids` tfvar → annotation. Confirm tfvars |
| Route53 ALIAS records return NXDOMAIN | `EvaluateTargetHealth=true` returns empty until ALB has healthy targets | Wait for target groups to register healthy; verify with `aws elbv2 describe-target-health`. If an ingress is missing, `helm get manifest <release> | kubectl apply -f -` |
| `curl https://...` works but Python httpx fails with `nodename nor servname` | macOS `mDNSResponder` cached the negative DNS result from before records existed | `sudo killall -HUP mDNSResponder` |
| Claude Code OAuth completes, then `HTTP Connection failed: Session not found` | FastMCP per-pod session state; ALB round-robins, breaks stateful protocol with replicas>1 | Already mitigated: `locals.tf` sets `FASTMCP_STATELESS_HTTP=1` in `mcp_env_overlay` |
| Azure AD: "Your reply url contains prohibited words or prohibited domains" | Hostname contains `microsoft` (or `azure`, `office`, `live`, `windows`) | Use `hostname_prefix = "ms-graph"` for the Microsoft MCP |
| AS log: `Upstream token exchange failed: HTTP 401` | Wrong/missing upstream client_secret | For Cognito PKCE-only: secret MUST be empty string `""`. For Okta confidential: real secret |
| Cognito returns "invalid scope" | Client doesn't allow the requested scope | Match `upstream_scopes` to what your Cognito client lists under Allowed OAuth scopes (typically `openid email`) |
| `POST /oauth/token` returns 500 with `python-multipart` error | Auth image rebuilt without the dep | Already mitigated; if you see this on a fork, confirm `auth/pyproject.toml` includes `python-multipart` |

## File reference

```
Makefile                      # deploy-check / deploy-plan / deploy / deploy-status / smoke
                              #   (TF_ENV ?= dev; DEPLOY_UNSAFE=1 skips the git guards)

deployment/
  terraform-existing-vpc/
    aurora.tf                 # Aurora Serverless v2 + parameter group
    backend.tf                # Remote state: S3 bucket + DynamoDB lock table
    build-stages.tf           # Content-hash image tags + buildx build/push during apply
    custom-domain.tf          # Wildcard ACM cert + DNS validation
    eks.tf                    # EKS cluster + node group + addons (before_compute=true on cni/kube-proxy)
    eks-lb-controller.tf      # ALB controller helm (passes vpcId/region explicitly)
    external-secrets.tf       # ESO helm + ClusterSecretStore (depends_on alb_controller)
    eks-domain.tf             # wait_for_alb + Route53 ALIAS records
    eks-validate.tf           # Seed preconditions (encryption_key, optional jwt_pk)
    iam.tf                    # IRSA for ESO + ALB controller
    secrets.tf                # SM secrets (encryption-key, db-credentials, as-credentials, per-provider oauth)
    services.tf               # for_each over services; one module.service per
    variables.tf              # Schema for jwt_verification + services + public_subnet_ids
    locals.tf                 # mcp_env_overlay (includes FASTMCP_STATELESS_HTTP=1)
    outputs.tf                # service_urls, deployed_image_tags, built_images
    environments/
      <env>.tfvars            # Your config (gitignored)
    modules/service/          # Per-service helm wrapper (sanitizes _, etc.)

deployment/helm/mcp-service/  # Chart used by every service (AS + MCPs)
  templates/
    deployment.yaml
    ingress.yaml              # Honors ingress.subnets when set
    externalsecret.yaml       # Per-secret ExternalSecret resources
    configmap.yaml            # MCP env (FASTMCP_STATELESS_HTTP lands here via mcp_env_overlay)
    _helpers.tpl              # envFrom helper (gates jwt secret correctly)
  values.yaml                 # Defaults + schema

scripts/
  smoke-deployed.py           # 22-check smoke against deployed URLs (driven by `make smoke`)

auth/
  pyproject.toml              # AS runtime deps (starlette, uvicorn, httpx, python-multipart)
  Dockerfile                  # auth image (used by both proxy and AS)
  auth/auth_server/           # AS endpoints, upstream OIDC bridge, JWT issuance
```

## Cost estimate

A single `dev` deploy at idle (no traffic):
- EKS control plane: $73/mo (regardless of node count)
- 1× t3.medium node group (2 nodes): ~$60/mo
- Aurora Serverless v2 @ 0.5 ACU min, ~idle: ~$45/mo
- ALB (1, shared across all MCPs): ~$22/mo
- NAT gateway egress: existing (VPC-shared) — $0
- Route53 hosted zone (existing): $0
- Secrets Manager (7 secrets): ~$3/mo
- ECR storage (~600 MB): <$0.10/mo

**~$200/mo idle**, scales linearly with traffic/nodes.

Reduce by:
- `aurora_min_capacity = 0.5` (already in template)
- Single-node group via `eks_node_min_count = 1, desired = 1, max = 1` (override in tfvars)
- Disable MCPs you don't need (`enabled = false`)

## Security notes

- All ingress is HTTPS-only (`ssl-redirect: '443'` on ingress; HTTP returns 308 to HTTPS).
- ACM wildcard cert is regional; same region as ALB.
- AWS Secrets Manager is encrypted with a customer-managed KMS key (created
  per environment).
- Aurora has TLS-required (`rds.force_ssl=1` in parameter group); the chart
  uses `sslmode=require` for the connection string.
- IRSA scopes ESO to read only the SM secrets we create (`bond-mcps-<env>-*`).
- DCR fail-closed: non-loopback HTTPS redirect URIs are rejected unless
  `as_allowed_redirect_hosts` lists the host. Default is loopback-only.
- AS-issued JWTs are RS256, audience-scoped per-MCP, 24h TTL (configurable
  via `jwt_verification.access_token_ttl_seconds`).
- `.gitignore` excludes `environments/*.tfvars` — verify after fork.
- The deployer's IAM role is the only path to AWS Secrets Manager; runtime
  secret material never lands in terraform state (we use `data.external`
  status probes rather than `data.aws_secretsmanager_secret_version`).

## CI

`.github/workflows/build-and-push.yml` ("Build images") builds every service
image on pull requests and on pushes to `main` as **validation only** — no
ECR push, no AWS credentials, no OIDC role. Its job is catching Dockerfile,
`.dockerignore` and dependency regressions before merge. Publishing is
`terraform apply`'s (§6), driven by `make deploy-plan && make deploy` from a
workstation.

CI publishing to ECR is deliberate future work. If you ever want it, the
wiring is:
1. Create a GitHub-Actions OIDC provider in your AWS account.
2. Create an IAM role trusted by it with `ecr:*` on
   `arn:aws:ecr:<region>:<account>:repository/bond-mcps-*` plus
   `ecr:GetAuthorizationToken` on `*`.
3. Set GitHub repo secret `AWS_PUSH_ROLE_ARN` = role ARN.
4. Set repo variable `AWS_REGION` (or rely on `us-west-2` default).

Note that a CI push only pre-warms ECR: the deploying apply still computes the
same content-hash tag and would skip the build it finds already pushed.
