# Platform Contract: shared EKS cluster (bond-ai + bond-mcps + sbel-crm)

This document is the cross-repo contract between **bond-ai**, **bond-mcps**,
and **sbel-crm**. An identical copy lives in all three repos
(`docs/PLATFORM-CONTRACT.md`). Any change to a value below is a breaking
change for the other repos: update all copies in the same change window, and
coordinate applies.

## Ownership

| Concern | Owner |
|---|---|
| EKS cluster (`bond-platform-dev`), node group, KMS | **bond-mcps** Terraform (`deployment/terraform-existing-vpc/`) |
| Cluster addons: AWS Load Balancer Controller, External Secrets Operator | **bond-mcps** Terraform |
| Shared ALB (via IngressGroup `bond-platform`) | AWS LB Controller (reconciled from all repos' Ingresses) |
| Namespace `bond-mcps` + its services | bond-mcps |
| Namespace `bond-ai` + the combined container workload | **bond-ai** Terraform, in "external cluster consume mode" (`eks_existing_cluster_name`) |
| Namespace `sbel-crm` + the combined container workload | **sbel-crm** Terraform (`deployment/terraform/`), consume mode like bond-ai |
| Route53 `ai.southbayequity.cloud` (apex A-alias) | bond-ai |
| Route53 `*.mcps.ai.southbayequity.cloud` | bond-mcps |
| Route53 `crm.southbayequity.cloud` (apex A-alias) | sbel-crm |

bond-ai and sbel-crm must NOT install a second LB controller or ESO. bond-mcps
must not rename or destroy the cluster without a coordinated window (the
consumers' `terraform plan` breaks silently otherwise).

## Pinned values

| Key | Value |
|---|---|
| Cluster name | `bond-platform-dev` |
| Region / VPC | `us-west-2` / `vpc-0a10b710daf789382` |
| IngressGroup (`alb.ingress.kubernetes.io/group.name`) | `bond-platform` |
| group.order allocation | auth-server = `1`, per-MCP services = `10`, bond-ai = `20`, sbel-crm = `30` |
| Node security group | bond-mcps output `node_security_group_id`; bond-ai and sbel-crm pin it in tfvars as `eks_external_node_security_group_id` (all repos use their own tfstate — no remote-state lookup) |
| ALB idle timeout | `300s` (set group-wide from bond-ai's Ingress **only** — no other member may set `alb.ingress.kubernetes.io/load-balancer-attributes`; conflicting group-wide attributes break reconciliation. Required for SSE) |
| Aurora access | each consumer's own Aurora SG allows 5432 from the node SG (sbel-crm: inline rule in its `security.tf`; bond-mcps: rule in its `eks.tf`) |

## In-cluster service DNS (consumed by bond-ai's nginx front door)

| Service | DNS | Port |
|---|---|---|
| Authorization Server | `auth-server.bond-mcps.svc.cluster.local` | 8001 |
| microsoft (ms-graph) | `microsoft.bond-mcps.svc.cluster.local` | 8000 |
| atlassian | `atlassian.bond-mcps.svc.cluster.local` | 8000 |
| github | `github.bond-mcps.svc.cluster.local` | 8000 |
| databricks | `databricks.bond-mcps.svc.cluster.local` | 8000 |

Renaming a service key in bond-mcps `services` tfvar changes its DNS name and
breaks bond-ai's nginx upstreams — treat service keys as part of this contract.

## Auth seam

| Key | Value |
|---|---|
| bond-ai → AS subject token | HS256, `iss=bond-ai`, `aud` contains `mcp-server`, `sub=email`, signed with bond-ai `JWT_SECRET_KEY` |
| Shared secret | bond-ai `JWT_SECRET_KEY` ≡ AS `BOND_MCPS_AS_BOND_JWT_SECRET` (Secrets Manager `${env}-as-credentials`). Rotate together. Only the AS pod holds it — never MCP pods. |
| Exchange | RFC 8693 on `POST {AS}/oauth/token`, `client_id=bond-ai`, `resource=<mcp url>`; AS resolves email → Cognito sub (pool of client `4uog2crm587odi3pb39e7b8726`) |
| Browser front door | `https://ai.southbayequity.cloud` — `BOND_MCPS_CONNECT_PUBLIC_URL` on every MCP pod; bond-ai nginx routes `/connect/<p>/*`, `/connections/<p>/callback`, `/connections/discovery` to the services above |

## MCP discovery

bond-ai learns which MCPs exist from one or more discovery endpoints
(`BOND_MCPS_DISCOVERY_URL` is a **comma-separated list**; earlier sources win
name collisions, and each source fails soft independently).

| Key | Value |
|---|---|
| Endpoint contract | `GET <base>/connections/discovery` → `{"mcps": [{"name", "display_name", "url", "description"?, "requires_connection"?}]}` (unauthenticated) |
| `requires_connection` | default `true` = managed: per-user connect flow delegated to the advertising service (`/connect/<name>/*`). `false` = authorized by the Bond JWT for any signed-in user — always connected, no connect flow (informational tile in bond-ai) |
| Sources (deployed) | bond-mcps AS `https://auth.mcps.ai.southbayequity.cloud/connections/discovery`, sbel-crm `https://crm.southbayequity.cloud/connections/discovery` |
| Sources (local combined mode) | `http://localhost:8000/connections/discovery` (bond-ai nginx → bond-mcps proxy :18000), `http://localhost:8001/connections/discovery` (sbel-crm backend) |
| sbel-crm advertised URL | `MCP_PUBLIC_URL` env on the sbel-crm pod (`https://crm.southbayequity.cloud/mcp/` — trailing slash; nginx `location = /mcp` 308-redirects the bare path) |

An MCP's `name` is a cross-repo identifier (bond-ai merges discovery onto its
static config by name) — renaming an advertised MCP is a breaking change.

## Change process

1. PR the change to this doc in **all three** repos first.
2. Apply bond-mcps before the consumers when a change affects cluster/addons;
   consumer-namespace-only changes need no coordination.
3. After any bond-mcps apply that could touch the node group or ALB, run
   `terraform plan` in bond-ai and sbel-crm and confirm both are clean before
   walking away.
