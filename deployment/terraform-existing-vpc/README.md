# bond-mcps EKS Terraform module

Deploys the bond-mcps auth proxy + N MCP servers to a new EKS cluster in an
existing VPC, with Aurora Postgres holding the encrypted token DB. Services
share one ALB via the AWS Load Balancer Controller's IngressGroup feature, and
secrets sync from AWS Secrets Manager via External Secrets Operator.

The chart at `deployment/helm/mcp-service/` describes one service (auth or any
MCP); this module instantiates it `for_each` over a `services` map in tfvars.
Adding a new MCP is one tfvars entry + one OAuth pre-registration + an image
push.

## Prerequisites

- **AWS account** with creds on hand (env vars or `~/.aws/credentials`) for the
  target region
- **Terraform >= 1.5** (1.5.7 verified)
- **AWS CLI v2** — used by the kubernetes/helm/kubectl providers' exec auth,
  and for `aws secretsmanager put-secret-value`
- **An existing VPC** with at least 2 private subnets in different AZs
- **A Route53 hosted zone** owning `var.base_domain`
- **Each MCP's OAuth app pre-registered** with its provider — callback URL
  `https://<svc>.<base_domain>/connections/<svc>/callback`
- **Local Python + Poetry** to run `bond-mcps generate-key` (one-time)

## First-time apply

The stack uses a phased apply because the kubernetes/helm providers can't
authenticate to a cluster that doesn't exist yet. From-scratch single-apply is
brittle; the three-step flow below is what we test against.

**1. Cluster + base AWS resources:**

```bash
terraform init
terraform apply -target=module.eks -var-file=environments/dev.tfvars
```

This creates the VPC data lookups, three KMS keys, the Aurora Serverless v2
cluster, all Secrets Manager shells (with placeholder values), the five ECR
repos, the wildcard ACM cert + DNS validation, and the EKS cluster itself
with one managed node group.

**2. Seed Secrets Manager values** (see next section). Skip and `terraform
apply` will fail loudly via `null_resource.encryption_key_seeded`.

**3. Push your service images to ECR.** Output `ecr_repository_urls` gives you
the URLs. `image_tag` in tfvars must match what CI pushes.

**4. Full apply:**

```bash
terraform apply -var-file=environments/dev.tfvars
```

Rolls out the ALB Load Balancer Controller, External Secrets Operator, the
two namespaces, every enabled service (one Helm release each), and the
Route53 ALIAS records pointing at the shared ALB.

## Secret seeding

### Encryption key — required (pods crash without it)

```bash
KEY=$(poetry --directory ../../auth run bond-mcps generate-key)

aws secretsmanager put-secret-value \
  --secret-id bond-mcps-dev-encryption-key \
  --region us-west-2 \
  --secret-string "{\"BOND_MCPS_ENCRYPTION_KEY\":\"$KEY\"}"
```

Store the value somewhere safe outside AWS — if it's lost, every cached OAuth
token in Aurora becomes unreadable.

### Per-MCP OAuth credentials — required for the MCP to do OAuth

JSON keys ARE the env var names the chart mounts (the chart uses ESO
`dataFrom.extract`, so SM JSON keys map 1:1 to env vars):

```bash
# GitHub
aws secretsmanager put-secret-value --secret-id bond-mcps-dev-github-oauth \
  --secret-string '{"GITHUB_CLIENT_ID":"…","GITHUB_CLIENT_SECRET":"…"}'

# Microsoft (tenant_id optional; "consumers" works for personal MSAs)
aws secretsmanager put-secret-value --secret-id bond-mcps-dev-microsoft-oauth \
  --secret-string '{"MS_CLIENT_ID":"…","MS_CLIENT_SECRET":"…","MS_TENANT_ID":"consumers"}'

# Atlassian (cloud_id optional — auto-discovered if absent)
aws secretsmanager put-secret-value --secret-id bond-mcps-dev-atlassian-oauth \
  --secret-string '{"ATLASSIAN_CLIENT_ID":"…","ATLASSIAN_CLIENT_SECRET":"…"}'

# Databricks (workspace-level OAuth app)
aws secretsmanager put-secret-value --secret-id bond-mcps-dev-databricks-oauth \
  --secret-string '{"DATABRICKS_CLIENT_ID":"…","DATABRICKS_CLIENT_SECRET":"…"}'
```

### DB credentials — seeded automatically

TF seeds this from the Aurora cluster's random password + endpoint. `lifecycle
{ ignore_changes = [secret_string] }` makes manual or SM-managed rotation safe
after first seed.

## Verifying the deploy

```bash
aws eks update-kubeconfig --name bond-mcps-dev-eks --region us-west-2

kubectl -n bond-mcps get pods             # 5 deployments, all Running
kubectl -n bond-mcps get ingress          # 5 ingresses, all sharing one ALB

curl https://auth.mcps.ai.southbayequity.cloud/health   # → {"status":"ok"}
```

If a pod is in `Init:CrashLoopBackoff`, check the preflight initContainer logs:

```bash
kubectl -n bond-mcps logs deploy/auth -c preflight
kubectl -n bond-mcps logs deploy/auth -c db-migrate    # auth only
```

The preflight runs `bond-mcps doctor`, which validates: DB reachable, schema
at Alembic head, encryption key round-trips, `BOND_MCPS_USER_ID` is set.
First failure points at exactly what's missing.

## Add a new MCP

1. **Source**: create `mcps/<name>/` with an entrypoint + Dockerfile (copy
   `mcps/github/Dockerfile` as template; expose container port 8000).
2. **OAuth provider**: register the app with callback
   `https://<name>.mcps.<base_domain>/connections/<name>/callback`. **Note**:
   this only works once plan section H/C1 lands (env-driven public redirect URI).
3. **Pre-create the ECR repo so CI has somewhere to push:**
   ```bash
   # Add the service entry to tfvars first, then:
   terraform apply -target='aws_ecr_repository.this["<name>"]'
   ```
4. **Push the image** via CI.
5. **Seed the OAuth SM secret** (see above).
6. **Apply:**
   ```bash
   terraform apply -var-file=environments/dev.tfvars
   ```

Adds the ExternalSecret, Helm release, Ingress, ALB rule, and Route53 ALIAS in
one apply.

## Deploy a single service

Set `enabled = false` on every service except the one you want, then
`terraform apply`. Removes Deployment, Service, Ingress, and DNS record for
the disabled ones. ECR repos and SM secrets persist (intentional — toggling
shouldn't force re-seeding).

Escape hatch (discouraged): `terraform apply -target='module.service["github"]'`.

## Teardown

```bash
terraform destroy -var-file=environments/dev.tfvars
```

Known gotchas:

- **ALB lingers if every service destroys at once.** If destroy hangs on the
  ALB, find it (`aws elbv2 describe-load-balancers --query 'LoadBalancers[?contains(LoadBalancerName, \`bond-mcps\`)]'`),
  delete manually, then retry.
- **ECR refuses delete with images present** unless `force_delete = true`.
  We mirror `aurora_deletion_protection`: dev (false) → force_delete, prod
  (true) → blocks until you empty the repo.
- **Aurora deletion_protection** blocks cluster destroy. Toggle
  `aurora_deletion_protection = false` first.
- **SM secrets with `recovery_window_in_days > 0`** soft-delete on destroy.
  Use `aws secretsmanager delete-secret --force-delete-without-recovery` to
  fully purge before recreating.

## Outputs reference

See `outputs.tf` for the full list. Highlights:

| Output | Use |
|---|---|
| `kubectl_config_cmd` | One-liner to configure kubectl |
| `service_urls` | Per-service public URLs |
| `auth_proxy_url` | Wire into bond-ai's `bond_mcp_config` tfvar |
| `secrets_manager_secret_names` | Use with `aws secretsmanager put-secret-value` |
| `ecr_repository_urls` | CI push targets |
| `needs_scaling_work_services` | Currently `["auth"]` — single-replica caveat |
| `shared_alb_dns_name` | The ALB every service shares (resolves after first apply) |

## Known limitations (v1)

1. **`BOND_MCPS_USER_ID` is one value per pod, not per request.** Single-tenant
   only. Multi-tenant requires the MCPs to derive `user_key` from the incoming
   Bearer JWT's `sub` claim. Tracked as plan section H/C2.
2. **OAuth redirect URI is hardcoded** to `http://localhost` in
   `auth/auth/proxy_client.py:107`. Deployed MCPs can't currently complete
   OAuth flows. Tracked as plan section H/C1 (one-line code fix: read
   `BOND_AUTH_PROXY_PUBLIC_URL` env).
3. **Aurora TLS** defaults to `sslmode=require` (encrypted, but no cert
   validation). Prod should use `verify-full` with the RDS CA bundle mounted
   in pods. Tracked as plan section H/C3.
4. **Auth proxy single-replica.** Holds in-memory pending OAuth state with a
   5-min TTL — multi-replica would silently break OAuth callbacks. Flagged
   via `needs_scaling_work = true` in tfvars; surfaces in
   `needs_scaling_work_services` output.
5. **First-apply DNS race.** `eks-domain.tf` waits 90s after services finish
   for the ALB controller to create the LB, then looks it up by tag. If the
   ALB takes longer (rare), re-run `terraform apply` — idempotent.
