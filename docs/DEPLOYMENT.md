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

- 1 Authorization Server + N MCPs share a single ALB (IngressGroup
  `bond-platform`, `var.ingress_group_name`). In the shared-platform topology
  (see `docs/PLATFORM-CONTRACT.md`) bond-ai's Ingress joins the same group on
  the same cluster (`bond-platform-<env>`), so one ALB serves bond-ai's apex
  host AND every `*.<base_domain>` MCP host (SNI, two certs).
- Aurora Postgres Serverless v2 (`bond_mcps` DB) stores encrypted upstream tokens.
- AWS Secrets Manager + ExternalSecrets Operator inject runtime secrets.
- All ingress HTTPS via one wildcard ACM cert for `*.<base_domain>`.

**Second caller population — bond-ai delegation (RFC 8693).** Besides Claude
Code's interactive flow above, the AS supports
`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`
(`auth/auth_server/token_exchange.py`): bond-ai presents its HS256 bond JWT
(secret shared ONLY with the AS via `BOND_MCPS_AS_BOND_JWT_SECRET` in the
as-credentials secret) plus `resource=<mcp url>`, and receives a short-lived
RS256 token with `sub` resolved to the Cognito sub (`cognito-idp:ListUsers`,
IRSA-scoped). MCP pods therefore verify exactly ONE population: RS256 via the
AS JWKS. Browser-facing connect flows ride bond-ai's front door
(`BOND_MCPS_CONNECT_PUBLIC_URL` on every MCP pod); the discovery manifest's
`name` must be the provider connect name (see `locals.tf`), not the hostname
prefix.

## TL;DR (10 steps)

```
1.  Fork repo, install tools, get AWS context.
2.  Create Cognito user pool + PKCE-only app client (or use Okta).
3.  Copy environments/dev.tfvars.example → environments/<env>.tfvars; edit.
4.  cd deployment/terraform-existing-vpc && terraform init && terraform apply -var-file=environments/<env>.tfvars
    → fails at encryption_key_seeded precondition (expected).
5.  Seed AWS Secrets Manager (encryption-key, as-credentials, per-provider OAuth secrets).
6.  ./scripts/build-and-push-ecr.sh 0.1.0   # builds + pushes all 5 images
7.  Add https://auth.<base>/oauth/upstream/callback to upstream IdP client.
8.  terraform apply -var-file=environments/<env>.tfvars   # second apply, completes.
9.  python scripts/smoke-deployed.py --as-url … --mcp-url …   # all green.
10. Per-provider OAuth app callback URLs, then claude mcp add + /mcp.
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
    image_tag       = "0.1.0"
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
    image_tag         = "0.1.0"
    # IMPORTANT: do NOT use "microsoft" as the hostname prefix. Entra ID
    # rejects reply URLs whose hostname contains Microsoft-trademark terms.
    hostname_prefix   = "ms-graph"
    replicas          = 2
    oauth_secret_name = "microsoft-oauth"
    # consumers = personal MSA; switch to your tenant GUID for org accounts.
    extra_env = { MS_TENANT_ID = "consumers" }
    health    = { type = "http", path = "/healthz" }
  }

  atlassian = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-atlassian"
    image_tag         = "0.1.0"
    hostname_prefix   = "atlassian"
    replicas          = 2
    oauth_secret_name = "atlassian-oauth"
    health            = { type = "http", path = "/healthz" }
  }

  github = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-github"
    image_tag         = "0.1.0"
    hostname_prefix   = "github"
    replicas          = 2
    oauth_secret_name = "github-oauth"
    health            = { type = "http", path = "/healthz" }
  }

  databricks = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-databricks"
    image_tag         = "0.1.0"
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

**Note on `.gitignore`:** the repo's `.gitignore` has an exception that *keeps*
`environments/*.tfvars` checked in. This is intentional — tfvars hold
identifiers (pool ID, subnet IDs, hosted zone ID), NOT secrets. Real secrets
live in AWS Secrets Manager. If your org policy forbids tracking these
identifiers, remove the `!deployment/terraform-existing-vpc/environments/*.tfvars`
line from `.gitignore`.

## 4. First terraform apply (expect to fail)

```bash
cd deployment/terraform-existing-vpc
export AWS_REGION=us-west-2          # MUST match your tfvars region
aws sts get-caller-identity          # confirm right account
terraform init
terraform plan -var-file=environments/<env>.tfvars -out=/tmp/plan-1.tfplan
# Review: should be ~100 resources for a fresh deploy
terraform apply /tmp/plan-1.tfplan
```

**Expected failure** (~15-20 min in):
```
Error: Resource precondition failed
  …encryption_key is not yet seeded (status: missing or placeholder).
```

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

## 6. Build + push images

ECR repos exist from step 4's first apply. Push images now.

```bash
cd <fork-root>
./scripts/build-and-push-ecr.sh 0.1.0 2>&1 | tee /tmp/build.log
```

The script:
- Logs into ECR via your local AWS creds
- Uses `docker buildx` with `--platform linux/amd64` (works from Apple Silicon Macs)
- Stages `_shared_auth_pkg/` under each MCP context (mirrors the CI workflow)
- Pushes all 5 images at the tag you pass

**Cross-arch build on Apple Silicon takes 15-30 min the first time.** Buildx
cache speeds up subsequent runs to <5 min.

Verify:
```bash
for r in bond-mcps-auth bond-mcps-mcp-microsoft bond-mcps-mcp-atlassian \
         bond-mcps-mcp-github bond-mcps-mcp-databricks; do
  printf "%-30s " "$r"
  aws --region us-west-2 ecr describe-images --repository-name "$r" \
    --image-ids imageTag=0.1.0 --query 'imageDetails[0].imageDigest' --output text
done
```

All five lines should print a `sha256:` digest.

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
cd deployment/terraform-existing-vpc
terraform plan -var-file=environments/<env>.tfvars -out=/tmp/plan-2.tfplan
terraform apply /tmp/plan-2.tfplan
```

This time it completes (~5-10 min). The 4 helm releases roll out, ALB
provisions, ACM cert validates, Route53 A records point to the ALB.

If you see `helm_release` failures, see Troubleshooting → "Helm releases stuck".

## 9. Smoke test

```bash
sudo killall -HUP mDNSResponder    # macOS only — flush negative DNS cache
                                    # (DNS records were just created; the OS
                                    # may have cached "doesn't exist")

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
When you change MCP code:
1. Bump the per-service `image_tag` in tfvars (`0.1.0` → `0.1.1`). **You must
   bump the tag**, not push over the existing tag — kubelet's
   `IfNotPresent` policy caches the old digest on every node.
2. `./scripts/build-and-push-ecr.sh 0.1.1`
3. `terraform plan -var-file=environments/<env>.tfvars -out=/tmp/plan.tfplan && terraform apply /tmp/plan.tfplan`

The chart's `image.pullPolicy=IfNotPresent` saves bandwidth on healthy pods;
the tag bump is what forces the pull.

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
| Pod CrashLoopBackOff: `ModuleNotFoundError: starlette` (or uvicorn / httpx / python-multipart) | AS image missing deps | Already mitigated in `auth/pyproject.toml`; rebuild image |
| Pod ImagePullBackOff: `<tag>: not found` | ECR repo exists but image not pushed for that tag | `./scripts/build-and-push-ecr.sh <tag>` |
| Pod still running old code after rebuild | `IfNotPresent` cached the old digest on kubelet | Bump `image_tag` (e.g. `0.1.0` → `0.1.1`), re-apply |
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
deployment/
  terraform-existing-vpc/
    aurora.tf                 # Aurora Serverless v2 + parameter group
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
    outputs.tf
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
  build-and-push-ecr.sh       # All 5 images, linux/amd64, ECR
  smoke-deployed.py           # 22-check smoke against deployed URLs

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

## CI (optional)

`.github/workflows/build-and-push.yml` builds + pushes on every merge to
main, tagging both `{sha}` and `latest`. Wire it up:
1. Create a GitHub-Actions OIDC provider in your AWS account.
2. Create an IAM role trusted by it with `ecr:*` on
   `arn:aws:ecr:<region>:<account>:repository/bond-mcps-*` plus
   `ecr:GetAuthorizationToken` on `*`.
3. Set GitHub repo secret `AWS_PUSH_ROLE_ARN` = role ARN.
4. Set repo variable `AWS_REGION` (or rely on `us-west-2` default).

The workflow does NOT update `image_tag` in tfvars — that's still manual or
via your own CD step.
