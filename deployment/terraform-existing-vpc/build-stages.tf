# Image builds run INSIDE terraform apply. The tag is a content hash of exactly
# what each Dockerfile COPYs, so `apply` is the single deploy verb: changed
# code => new tag => build + push => pods roll; unchanged code => no-op. There
# is no separate build script and no image_tag to hand-edit — keeping those two
# in sync by hand was the failure mode this replaces.
#
# Keyed by IMAGE, not by service key: auth_server (and the optional auth proxy)
# both run the bond-mcps-auth image, and sbel's image is built in another repo
# entirely (it keeps a hand-pinned image_tag — see local.effective_image_tag).
#
# The hash covers files on disk, not a git ref — a dirty tree or a feature
# branch ships silently. `make deploy-check` is the guard for that.

locals {
  repo_root = abspath("${path.module}/../..")

  # The vendored auth package. Every MCP image stages this into
  # <context>/_shared_auth_pkg/ before build (Dockerfiles do
  # `COPY _shared_auth_pkg/ /auth/`), so it is a hash input for all of them.
  # *.mako matters: alembic/script.py.mako is the one non-.py file in the tree.
  auth_pkg_hash = md5(join("", concat(
    [for f in fileset("${local.repo_root}/auth/auth", "**/*.py") :
    filemd5("${local.repo_root}/auth/auth/${f}")],
    [for f in fileset("${local.repo_root}/auth/auth", "**/*.mako") :
    filemd5("${local.repo_root}/auth/auth/${f}")],
    [
      filemd5("${local.repo_root}/auth/pyproject.toml"),
      filemd5("${local.repo_root}/auth/poetry.lock"),
    ],
  )))

  # Build matrix. src_dirs/src_files are relative to `context` and mirror each
  # Dockerfile's COPY lines exactly — tests/, *_cli.py and README.md are
  # .dockerignore'd, so hashing them would roll pods for changes that cannot
  # appear in the image.
  # NOTE: this key set must match the `build` validation in variables.tf
  # (validation blocks cannot read locals, so the list is duplicated there).
  build_matrix = {
    auth = {
      context           = "auth"
      dockerfile        = "auth/Dockerfile"
      needs_shared_auth = false
      src_dirs          = ["auth"]
      src_files         = ["pyproject.toml", "poetry.lock"]
    }
    microsoft = {
      context           = "mcps/microsoft"
      dockerfile        = "mcps/microsoft/Dockerfile"
      needs_shared_auth = true
      src_dirs          = ["ms_graph"]
      src_files         = ["pyproject.toml", "poetry.lock", "ms_graph_mcp.py"]
    }
    atlassian = {
      context           = "mcps/atlassian"
      dockerfile        = "mcps/atlassian/Dockerfile"
      needs_shared_auth = true
      src_dirs          = ["atlassian"]
      src_files         = ["pyproject.toml", "poetry.lock", "atlassian_mcp.py"]
    }
    github = {
      context           = "mcps/github"
      dockerfile        = "mcps/github/Dockerfile"
      needs_shared_auth = true
      src_dirs          = ["github"]
      src_files         = ["pyproject.toml", "poetry.lock", "github_mcp.py"]
    }
    databricks = {
      context           = "mcps/databricks"
      dockerfile        = "mcps/databricks/Dockerfile"
      needs_shared_auth = true
      src_dirs          = ["dbx"]
      src_files         = ["pyproject.toml", "poetry.lock", "databricks_mcp.py"]
    }
  }

  # 12-char md5 per image. fileset() returns a sorted set, so the join order
  # is deterministic across machines.
  image_tags = {
    for bk, b in local.build_matrix : bk => substr(md5(join("", concat(
      [filemd5("${local.repo_root}/${b.dockerfile}")],
      [for f in b.src_files : filemd5("${local.repo_root}/${b.context}/${f}")],
      flatten([for d in b.src_dirs : [
        for f in fileset("${local.repo_root}/${b.context}/${d}", "**/*.py") :
        filemd5("${local.repo_root}/${b.context}/${d}/${f}")
      ]]),
      flatten([for d in b.src_dirs : [
        for f in fileset("${local.repo_root}/${b.context}/${d}", "**/*.mako") :
        filemd5("${local.repo_root}/${b.context}/${d}/${f}")
      ]]),
      b.needs_shared_auth ? [local.auth_pkg_hash] : [],
      # The hash covers files, not the python:3.12-slim base image or PyPI/apt
      # resolution inside the build. Bump var.force_rebuild to mint fresh tags
      # (a new tag can never hit the skip-if-exists path below).
      [var.force_rebuild],
    ))), 0, 12)
  }

  # Only build images that at least one ENABLED service consumes (databricks
  # is enabled=false today => no build resource; its tag is still computed).
  active_build_keys = toset([
    for k, v in local.enabled_services : v.build if v.build != null
  ])
  active_builds = { for bk in local.active_build_keys : bk => local.build_matrix[bk] }

  # ECR repos are keyed by SERVICE key (ecr.tf), and 1..n services can share
  # one image (auth_server + the optional auth proxy). Any consumer's repo
  # works — same image_repo_name means the same repository URL.
  build_repo_service_key = {
    for bk in local.active_build_keys :
    bk => [for k, v in var.services : k if v.build == bk][0]
  }

  # The tag each SERVICE actually deploys: computed hash for repo-built
  # services, the hand-pinned image_tag for foreign images (sbel). The
  # variables.tf validation guarantees exactly one of the two is set.
  effective_image_tag = {
    for k, v in var.services :
    k => v.build != null ? local.image_tags[v.build] : v.image_tag
  }
}

resource "null_resource" "build" {
  for_each = local.active_builds

  depends_on = [
    aws_ecr_repository.this,
    # Fail the fresh-fork bootstrap BEFORE spending 15-30 min on cross-arch
    # builds for an apply that cannot complete. On an established stack this
    # edge is already satisfied and costs nothing.
    terraform_data.encryption_key_seeded,
  ]

  triggers = {
    image_tag = local.image_tags[each.key]
    repo_url  = aws_ecr_repository.this[local.build_repo_service_key[each.key]].repository_url
    repo_name = aws_ecr_repository.this[local.build_repo_service_key[each.key]].name
  }

  provisioner "local-exec" {
    working_dir = local.repo_root
    interpreter = ["/bin/bash", "-c"]

    # Tag/repo come from self.triggers via `environment` — NOT the locals —
    # so a saved plan applied after the tree moved still pushes exactly the
    # tag the plan (and the helm_release values) recorded. Provisioner bodies
    # are re-evaluated at apply time; resource attributes are not.
    environment = {
      TAG       = self.triggers.image_tag
      REPO_URL  = self.triggers.repo_url
      REPO_NAME = self.triggers.repo_name
    }

    command = <<-EOT
      set -euo pipefail

      BUILD_KEY="${each.key}"
      CONTEXT="${each.value.context}"
      DOCKERFILE="${each.value.dockerfile}"
      NEEDS_SHARED="${each.value.needs_shared_auth}"
      REGION="${var.aws_region}"
      ECR_REGISTRY="$${REPO_URL%%/*}"

      docker info > /dev/null 2>&1 || { echo "Error: Docker daemon is not running"; exit 1; }

      # ECR login. Clear stale creds first: on macOS the Keychain credential
      # helper hands docker an expired ECR token and the push 401s.
      echo "==> [$BUILD_KEY] ECR login at $ECR_REGISTRY"
      docker logout "$ECR_REGISTRY" 2>/dev/null || true
      if [[ "$OSTYPE" == "darwin"* ]]; then
        while security delete-internet-password -s "$ECR_REGISTRY" 2>/dev/null; do :; done
      fi
      aws ecr get-login-password --region "$REGION" \
        | docker login --username AWS --password-stdin "$ECR_REGISTRY"

      # Content hash already in ECR => identical inputs => nothing to build.
      if aws ecr describe-images --repository-name "$REPO_NAME" --region "$REGION" \
           --image-ids imageTag="$TAG" > /dev/null 2>&1; then
        echo "==> [$BUILD_KEY] $TAG already exists in ECR — skipping build"
        exit 0
      fi

      # Dedicated builder, addressed via --builder so the operator's current
      # buildx builder selection is never touched.
      docker buildx inspect bond-mcps-builder > /dev/null 2>&1 \
        || docker buildx create --name bond-mcps-builder > /dev/null

      # MCP Dockerfiles resolve `bond-auth = {path = "../../auth"}` to /auth
      # via `COPY _shared_auth_pkg/ /auth/`. Stage it fresh every time (a
      # stale leftover is invisible to the content hash) and always clean up.
      if [ "$NEEDS_SHARED" = "true" ]; then
        SHARED="$CONTEXT/_shared_auth_pkg"
        trap 'rm -rf "$SHARED"' EXIT
        rm -rf "$SHARED"
        mkdir -p "$SHARED"
        cp -R auth/auth "$SHARED/auth"
        cp auth/pyproject.toml auth/poetry.lock "$SHARED/"
      fi

      GIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo unknown)
      if [ -n "$(git status --porcelain 2>/dev/null)" ]; then GIT_SHA="$GIT_SHA-dirty"; fi

      echo "==> [$BUILD_KEY] buildx --platform linux/amd64 -> $REPO_URL:$TAG"
      docker buildx build \
        --builder bond-mcps-builder \
        --platform linux/amd64 \
        --file "$DOCKERFILE" \
        --label "org.opencontainers.image.revision=$GIT_SHA" \
        --label "org.opencontainers.image.source=https://github.com/jmcarnahan/bond-mcps" \
        --tag "$REPO_URL:$TAG" \
        --push \
        "$CONTEXT"

      aws ecr describe-images --repository-name "$REPO_NAME" --region "$REGION" \
        --image-ids imageTag="$TAG" > /dev/null 2>&1 \
        || { echo "Error: $REPO_NAME:$TAG not found in ECR after push"; exit 1; }
      echo "==> [$BUILD_KEY] verified $REPO_NAME:$TAG"
    EOT
  }
}
