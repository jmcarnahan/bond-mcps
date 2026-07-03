# Platform Contract: shared EKS cluster (bond-ai + bond-mcps)

This document is the cross-repo contract between **bond-ai** and **bond-mcps**.
An identical copy lives in both repos (`docs/PLATFORM-CONTRACT.md`). Any change
to a value below is a breaking change for the other repo: update both copies in
the same change window, and coordinate applies.

## Ownership

| Concern | Owner |
|---|---|
| EKS cluster (`bond-platform-dev`), node group, KMS | **bond-mcps** Terraform (`deployment/terraform-existing-vpc/`) |
| Cluster addons: AWS Load Balancer Controller, External Secrets Operator | **bond-mcps** Terraform |
| Shared ALB (via IngressGroup `bond-platform`) | AWS LB Controller (reconciled from both repos' Ingresses) |
| Namespace `bond-mcps` + its 5 services | bond-mcps |
| Namespace `bond-ai` + the combined container workload | **bond-ai** Terraform, in "external cluster consume mode" (`eks_existing_cluster_name`) |
| Route53 `ai.southbayequity.cloud` (apex A-alias) | bond-ai |
| Route53 `*.mcps.ai.southbayequity.cloud` | bond-mcps |

bond-ai must NOT install a second LB controller or ESO. bond-mcps must not
rename or destroy the cluster without a coordinated window (bond-ai's
`terraform plan` breaks silently otherwise).

## Pinned values

| Key | Value |
|---|---|
| Cluster name | `bond-platform-dev` |
| Region / VPC | `us-west-2` / `vpc-0a10b710daf789382` |
| IngressGroup (`alb.ingress.kubernetes.io/group.name`) | `bond-platform` |
| group.order allocation | auth-server = `1`, per-MCP services = `10`, bond-ai = `20` |
| Node security group | bond-mcps output `node_security_group_id`; bond-ai pins it in tfvars as `eks_external_node_security_group_id` (both repos use local tfstate — no remote-state lookup) |
| ALB idle timeout | `300s` (set group-wide from bond-ai's Ingress; required for SSE) |

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

## Change process

1. PR the change to this doc in **both** repos first.
2. Apply bond-mcps before bond-ai when a change affects cluster/addons;
   reverse order when it only affects bond-ai's namespace.
3. After any bond-mcps apply that could touch the node group or ALB, run
   `terraform plan` in bond-ai and confirm it is clean before walking away.
