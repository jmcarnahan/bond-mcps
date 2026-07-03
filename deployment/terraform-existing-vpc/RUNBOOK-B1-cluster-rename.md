# B1 Runbook — cluster rename to `bond-platform-dev` (coordinated downtime)

Executes the cluster rebuild prepared on branch `feat-platform-dev-cluster`.
Expected downtime for all bond-mcps services (incl. Claude Code MCP access):
**~30–60 min**. Aurora, Secrets Manager, ECR, ACM, and Route53 zones survive
(verified by plan review 2026-07-03: 34 add / 16 change / 31 destroy, every
destroy a `-/+` replacement inside `module.eks` + `null_resource.wait_for_alb`).

## Pre-flight (before the window — no downtime)

1. **Merge PRs to main**: `feat-as-token-exchange` (exchange grant — required
   in the 0.2.0 image), then `feat-platform-dev-cluster` (this infra).
2. **Build + push the auth image from main**:
   ```bash
   cd ~/projects/bond-mcps && git checkout main && git pull
   aws --region us-west-2 ecr get-login-password | docker login --username AWS \
     --password-stdin 119684128788.dkr.ecr.us-west-2.amazonaws.com
   docker buildx build --platform linux/amd64 -f auth/Dockerfile \
     -t 119684128788.dkr.ecr.us-west-2.amazonaws.com/bond-mcps-auth:0.2.0 --push auth
   aws --region us-west-2 ecr describe-images --repository-name bond-mcps-auth \
     --image-ids imageTag=0.2.0 --query 'imageDetails[0].imageDigest' --output text
   ```
   (MCP service images stay on their current tag.)
3. **Seed the exchange secret** (merge, don't overwrite — the live JSON holds
   the AS RSA signing key):
   ```bash
   cur=$(aws --region us-west-2 secretsmanager get-secret-value \
     --secret-id bond-mcps-dev-as-credentials --query SecretString --output text)
   # BOND_AI_JWT_SECRET_KEY = the jwt_secret_key value from bond-ai's
   # app-config secret (bond-ai-dev-app-config-*).
   echo "$cur" | jq --arg s "$BOND_AI_JWT_SECRET_KEY" \
     '. + {BOND_MCPS_AS_BOND_JWT_SECRET: $s}' > /tmp/as-cred.json
   aws --region us-west-2 secretsmanager put-secret-value \
     --secret-id bond-mcps-dev-as-credentials \
     --secret-string file:///tmp/as-cred.json && rm /tmp/as-cred.json
   ```
4. **Fresh state backup**:
   `cp terraform.tfstate ~/tf-state-backups/bond-mcps/terraform.tfstate.pre-b1-$(date +%Y%m%d-%H%M)`
5. Announce the window to Claude Code users.

## Window

6. **Drop in-state k8s objects** (they die with the old cluster; refreshing
   them against a replaced cluster is what breaks one-shot plans):
   ```bash
   terraform state list | grep -E '^(kubernetes_|helm_release\.|kubectl_)|\.(kubernetes_|helm_release\.|kubectl_)' \
     > /tmp/k8s-state-list.txt   # review it first
   xargs -n1 terraform state rm < /tmp/k8s-state-list.txt
   ```
7. **Phased apply**:
   ```bash
   terraform apply -var-file=environments/dev.tfvars -target=module.eks
   terraform apply -var-file=environments/dev.tfvars   # everything else
   ```
   (Phase 1 ≈ 15–20 min cluster + node group; phase 2 recreates LB controller,
   ESO, namespaces, 5 helm releases, IRSA, and re-points Route53 `*.mcps`
   aliases at the new ALB.)

## Verify

- `aws eks update-kubeconfig --name bond-platform-dev --region us-west-2`
- `kubectl get pods -n bond-mcps` → 9 pods Ready (auth-server ×1, 4 MCPs ×2).
- `curl -s https://auth.mcps.ai.southbayequity.cloud/.well-known/oauth-authorization-server | jq .grant_types_supported`
  → includes `urn:ietf:params:oauth:grant-type:token-exchange`.
- JWKS unchanged (Claude Code tokens stay valid — signing key came from SM):
  `curl -s https://auth.mcps.ai.southbayequity.cloud/.well-known/jwks.json | jq '.keys[].kid'`
  matches pre-window kids.
- Exchange happy path: mint an HS256 subject token with bond-ai's
  JWT_SECRET_KEY (`iss=bond-ai`, `aud=["mcp-server"]`, `sub=<your email>`,
  5-min exp) and POST to `/oauth/token` with
  `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`,
  `resource=https://ms-graph.mcps.ai.southbayequity.cloud/mcp`,
  `client_id=bond-ai` → 200, and the returned JWT's `sub` is your Cognito sub.
- Claude Code regression: list/call a tool on `ms-graph.mcps…/mcp`.
- Record `terraform output node_security_group_id` → pin into bond-ai tfvars
  (`eks_external_node_security_group_id`) per docs/PLATFORM-CONTRACT.md.

## Rollback

Revert the cluster-name change in dev.tfvars, repeat steps 6–7. Same downtime
class. State backup from step 4 is the last resort
(`cp` it back over terraform.tfstate).
