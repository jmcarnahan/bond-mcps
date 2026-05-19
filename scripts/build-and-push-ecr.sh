#!/usr/bin/env bash
# Build all 5 bond-mcps images (linux/amd64) and push to ECR.
#
# Why this exists: GitHub Actions handles CI builds on push-to-main, but for
# local iteration against a deployed cluster (or before the PR lands) we need
# to push images straight from a developer machine. The MCP Dockerfiles use a
# path-dep on `bond-auth = { path = "../../auth" }` which resolves to
# /_shared_auth_pkg inside the build context — same trick the CI workflow uses.
#
# Usage:
#   ./scripts/build-and-push-ecr.sh [tag]
#
# Default tag is "0.1.0" (matches environments/dev.tfvars). Override per
# release: `./scripts/build-and-push-ecr.sh 0.2.0`.
#
# Required env (or defaults):
#   AWS_REGION       — defaults to us-west-2
#   AWS_ACCOUNT_ID   — auto-detected via `aws sts get-caller-identity` if unset

set -euo pipefail

TAG="${1:-0.1.0}"
AWS_REGION="${AWS_REGION:-us-west-2}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> registry: $REGISTRY"
echo "==> tag:      $TAG"
echo

# 1. ECR login (idempotent — docker just refreshes the token).
echo "==> ECR login"
aws --region "$AWS_REGION" ecr get-login-password \
  | docker login --username AWS --password-stdin "$REGISTRY"

# 2. Ensure a buildx builder exists. The default "desktop-linux" builder on
#    Docker Desktop already supports multi-arch via QEMU, so we just need to
#    make sure ONE is active.
if ! docker buildx inspect bond-mcps-builder >/dev/null 2>&1; then
  echo "==> creating buildx builder"
  docker buildx create --name bond-mcps-builder --use >/dev/null
else
  docker buildx use bond-mcps-builder
fi

# Service matrix: name, ecr repo, build context, dockerfile, needs_shared_auth
services=(
  "auth          bond-mcps-auth           auth            auth/Dockerfile            false"
  "microsoft     bond-mcps-mcp-microsoft  mcps/microsoft  mcps/microsoft/Dockerfile  true"
  "atlassian     bond-mcps-mcp-atlassian  mcps/atlassian  mcps/atlassian/Dockerfile  true"
  "github        bond-mcps-mcp-github     mcps/github     mcps/github/Dockerfile     true"
  "databricks    bond-mcps-mcp-databricks mcps/databricks mcps/databricks/Dockerfile true"
)

cleanup_paths=()
# shellcheck disable=SC2154  # `p` is bound inside the trap's own for-loop
trap 'for p in "${cleanup_paths[@]}"; do rm -rf "$p"; done' EXIT

for entry in "${services[@]}"; do
  read -r name repo context dockerfile needs_shared <<<"$entry"

  if [[ "$needs_shared" == "true" ]]; then
    shared="$context/_shared_auth_pkg"
    rm -rf "$shared"
    mkdir -p "$shared"
    cp -R auth/auth "$shared/auth"
    cp auth/pyproject.toml "$shared/pyproject.toml"
    cp auth/poetry.lock "$shared/poetry.lock"
    cleanup_paths+=("$shared")
  fi

  echo
  echo "==> [$name] build + push $REGISTRY/$repo:$TAG"
  docker buildx build \
    --platform linux/amd64 \
    --file "$dockerfile" \
    --tag "$REGISTRY/$repo:$TAG" \
    --push \
    "$context"
done

echo
echo "==> Done. Five images pushed at tag $TAG."
