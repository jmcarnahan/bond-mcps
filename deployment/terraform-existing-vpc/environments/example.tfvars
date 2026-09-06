# Template tfvars. Copy to environments/<env>.tfvars and fill the REQUIRED
# fields; all OPTIONAL fields have sensible defaults declared in variables.tf.
#
# Quick-start cheat sheet for a multi-tenant (Claude Code) deploy:
#   1. Set existing_vpc_id, base_domain, hosted_zone_id below.
#   2. Set jwt_verification.* below (Cognito or Okta coords).
#   3. Add ms-graph / atlassian / github / databricks blocks to `services` as
#      you need them, plus the `auth_server` block (mandatory in JWT mode).
#   4. `terraform apply` once → it'll fail on the seed checks; that tells you
#      which AWS SM secrets to put-secret-value next.
#   5. Re-apply.
#   6. Update Cognito (or Okta) app client callback URLs to include
#      https://<auth-hostname>/oauth/upstream/callback.
#   7. Update each per-provider OAuth app's redirect URI allowlist to include
#      https://<mcp-hostname>/connect/<provider>/callback.
#   8. `claude mcp add --transport http <name> https://<mcp-hostname>/mcp`.

# ============================================================
# REQUIRED
# ============================================================

environment = "dev" # short env name, goes into resource names

existing_vpc_id    = "vpc-xxxxxxxx"                         # VPC ID to deploy into
private_subnet_ids = ["subnet-xxxxxxxx", "subnet-yyyyyyyy"] # 2+ AZs; NAT egress required (Aurora + EKS nodes)
public_subnet_ids  = ["subnet-aaaaaaaa", "subnet-bbbbbbbb"] # 2+ AZs; shared ALB lives here. Required when the
# VPC's public subnets don't carry the
# kubernetes.io/role/elb tag (we don't tag the
# shared VPC — we pass IDs explicitly via this var).
base_domain    = "mcps.example.com" # services exposed as <prefix>.<base_domain>
hosted_zone_id = "Zxxxxxxxxxxxx"    # Route53 zone owning base_domain

# ============================================================
# JWT verification + upstream IdP — REQUIRED for Claude Code support
# ============================================================
# When enabled, each MCP becomes an OAuth 2.1 Resource Server and the
# bond-mcps Authorization Server (declared in `services` below) issues JWTs.
# Disable (default) to run the legacy single-tenant flow without the AS.
#
# The Cognito coordinates below reuse the bond-ai User Pool. To use a
# separate pool, replace upstream_issuer + upstream_client_id and add the
# new callback URL to that pool's app client.
jwt_verification = {
  enabled = true

  # JWKS URL — defaults to https://<auth-hostname>/.well-known/jwks.json
  # when left empty. Override only when the AS lives at a different URL.
  jwks_uri    = ""
  issuer      = "" # defaults to as_base_url when empty
  as_base_url = "" # defaults to https://<auth-hostname> when empty

  # Upstream OIDC IdP (Cognito or Okta). All four REQUIRED when enabled=true.
  upstream_idp          = "cognito"
  upstream_issuer       = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_xxxxxxxxx"
  upstream_client_id    = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  upstream_redirect_uri = "https://auth.mcps.example.com/oauth/upstream/callback"

  # Optional: CSV of allowed scopes from the upstream IdP.
  # upstream_scopes        = "openid email profile"
  # Optional: CSV of email domains to gate sign-up.
  # upstream_allowed_domains = "acme.com"

  # CSV of HTTPS hosts permitted as DCR redirect_uri targets, in addition
  # to loopback. Required for any Claude Code deployment behind a corp
  # https proxy; for stock localhost-loopback Claude Code, leave empty.
  as_allowed_redirect_hosts = ""

  # Lengthen / shorten AS-issued JWT TTL (seconds). 86400 = 24h default
  # (matches bond-ai). Bump if Claude Code's refresh bug (#5706) annoys
  # users; lower if you want a tighter revocation window.
  # access_token_ttl_seconds = 86400
}

# ============================================================
# Services
# ============================================================
# Each entry deploys one pod + ALB rule + ECR repo. The `auth_server` entry
# below MUST be present when jwt_verification.enabled=true. The `auth_proxy`
# entry is now OPTIONAL — only useful for laptop/CLI flows; deployed clusters
# can omit it.

services = {
  # --- Authorization Server (REQUIRED when jwt_verification.enabled=true) ---
  auth_server = {
    enabled         = true
    image_repo_name = "bond-mcps-auth" # shares the auth/ image with the proxy
    build           = "auth"           # image built by terraform apply (build-stages.tf); tag = content hash
    hostname_prefix = "auth"           # → auth.<base_domain>
    container_port  = 8001             # AS binds 8001
    replicas        = 1                # AS is stateless-with-DB; >1 is safe but unneeded
    is_auth_server  = true
    runs_migrations = true # AS runs `bond-mcps migrate-db` on start
    command         = ["python", "-m", "auth.auth_server", "--host", "0.0.0.0", "--port", "8001"]
    health          = { type = "http", path = "/healthz" }
    # No oauth_secret_name — terraform auto-creates ${prefix}as-credentials
    # for AS services. After first apply, populate that SM secret with
    # {"BOND_MCPS_AS_PRIVATE_KEY_PEM": "<RSA PEM>",
    #  "BOND_MCPS_UPSTREAM_CLIENT_SECRET": ""}.
    # Empty client_secret = public OIDC client (PKCE-only, e.g. Cognito
    # without "Generate client secret"). Fill in for Okta confidential apps.
  }

  # --- Per-provider MCP servers ---
  # Each pulls its provider OAuth credentials from the SM secret named
  # `${env}-${oauth_secret_name}`. After first apply, populate each secret
  # with the provider's client_id + client_secret per the README.

  # GitHub MCP
  # GitHub OAuth apps allow ONE callback URL per app — you must register a
  # fresh OAuth app for each environment you deploy. Reusing an app wired to
  # a different URL (e.g. localhost from local dev) will fail at OAuth time.
  github = {
    enabled           = false # set true after registering a new OAuth app
    image_repo_name   = "bond-mcps-mcp-github"
    build             = "github"
    hostname_prefix   = "github"
    replicas          = 2
    oauth_secret_name = "github-oauth"
    health            = { type = "http", path = "/healthz" }
  }

  # Microsoft Graph MCP
  # IMPORTANT: hostname_prefix MUST NOT contain "microsoft" (or "azure",
  # "office", "live", "windows"). Microsoft Entra ID's anti-impersonation
  # check rejects reply URLs whose hostname includes those terms when the
  # domain isn't *.microsoft.com. Use "ms-graph" as below.
  ms_graph = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-microsoft"
    build             = "microsoft"
    hostname_prefix   = "ms-graph"
    replicas          = 2
    oauth_secret_name = "microsoft-oauth"
    extra_env = {
      # Use "consumers" for personal MSA, "common" for any AAD, or a tenant GUID.
      MS_TENANT_ID = "consumers"
      # Hide mail from senders outside these domains (unset = off). List every
      # domain the org sends from, including <tenant>.onmicrosoft.com.
      # MS_MAIL_ALLOWED_SENDER_DOMAINS = "yourcompany.com,yourcompany.onmicrosoft.com"
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

  databricks = {
    enabled           = false # set true after registering OAuth app OR providing a PAT
    image_repo_name   = "bond-mcps-mcp-databricks"
    build             = "databricks"
    hostname_prefix   = "databricks"
    replicas          = 1
    oauth_secret_name = "databricks-oauth"
    extra_env = {
      DATABRICKS_HOST      = "https://<workspace-id>.cloud.databricks.com"
      DATABRICKS_HTTP_PATH = "/sql/1.0/warehouses/<warehouse-id>"
    }
    health = { type = "http", path = "/healthz" }
  }

  # --- Foreign-image data service (OPTIONAL — example: sbel) ---
  # A service whose image is built OUTSIDE this repo can still ride the
  # platform. Three per-service fields exist for that case:
  #   db_secret_name         — the service gets its OWN logical database on
  #                            the shared Aurora cluster. Terraform creates
  #                            the SM secret shell `${env-prefix}<name>`
  #                            (host/port pre-seeded from the cluster);
  #                            create the DB + role, then put-secret-value
  #                            with real username/password/dbname.
  #   preflight_enabled      — set false when the image lacks the
  #                            `bond-mcps` CLI (the default preflight init
  #                            container runs `bond-mcps doctor`).
  #   exclude_from_discovery — set true for services with no OAuth /connect
  #                            flow, so they don't appear in the AS's
  #                            /connections/discovery manifest (which drives
  #                            bond-ai's provider-connect UI).
  #
  # sbel = {
  #   enabled                = true
  #   image_repo_name        = "bond-mcps-mcp-sbel" # built from the sbel repo
  #   image_tag              = "0.1.1"            # hand-pinned BY DESIGN: foreign image, terraform cannot build it
  #   hostname_prefix        = "sbel"
  #   container_port         = 8080
  #   replicas               = 1
  #   db_secret_name         = "sbel-db"
  #   oauth_secret_name      = "sbel-admin" # arbitrary env vars via SM (e.g. an admin key)
  #   preflight_enabled      = false
  #   exclude_from_discovery = true
  #   health                 = { type = "http", path = "/healthz" }
  # }

  # --- Legacy auth proxy (OPTIONAL — only needed for laptop/CLI dev flows) ---
  # Uncomment to deploy. In JWT mode the proxy is unused; leaving it out is
  # the recommended path.
  #
  # auth = {
  #   enabled            = true
  #   image_repo_name    = "bond-mcps-auth"
  #   build              = "auth" # NOTE: uncommenting also needs ecr.tf de-duped (two services would declare repo bond-mcps-auth)
  #   hostname_prefix    = "auth-proxy"
  #   replicas           = 1
  #   is_auth_proxy      = true
  #   needs_scaling_work = true # auth proxy holds in-memory state; >1 replica breaks OAuth
  #   health             = { type = "http", path = "/health" }
  # }
}

# ============================================================
# OPTIONAL (defaults in variables.tf)
# ============================================================

# aws_region                   = "us-west-2"
# private_subnet_ids           = []                # auto-discovers private subnets
# aurora_min_capacity          = 1.0
# aurora_max_capacity          = 2
# aurora_deletion_protection   = true
# secrets_recovery_window_days = 0                 # 0 = immediate (dev); use 7+ for prod
# eks_kubernetes_version       = "1.31"
# eks_node_instance_type       = "t3.medium"
# eks_node_min_count           = 1
# eks_node_desired_count       = 2
# eks_node_max_count           = 3
