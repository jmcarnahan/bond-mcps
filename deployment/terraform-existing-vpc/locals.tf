locals {
  # ============================================================
  # Naming + tagging
  # ============================================================
  name_prefix = "${var.project_name}-${var.environment}"

  common_tags = {
    Project        = var.project_name
    Environment    = var.environment
    ManagedBy      = "Terraform"
    DeploymentType = "existing-vpc"
  }

  # ============================================================
  # Subnets
  # ============================================================
  private_subnet_ids = length(var.private_subnet_ids) > 0 ? var.private_subnet_ids : data.aws_subnets.private.ids

  # ============================================================
  # Service derivations
  # ============================================================

  # Services with enabled=true. for_each consumers use this.
  enabled_services = { for k, v in var.services : k => v if v.enabled }

  # Services that declared an oauth_secret_name (and didn't leave it null/empty).
  oauth_services = {
    for k, v in var.services : k => v
    if v.oauth_secret_name != null && try(v.oauth_secret_name, "") != ""
  }

  # Per-service hostname. Always computed (not gated by enabled) so disabling
  # a service doesn't churn the hostname map for other services.
  service_hostnames = {
    for k, v in var.services : k => "${v.hostname_prefix}.${var.base_domain}"
  }

  # Service identified as the auth proxy. Validation in variables.tf ensures
  # at most one — local OAuth callback relay; optional in deployed JWT mode.
  auth_proxy_key      = one([for k, v in var.services : k if v.is_auth_proxy])
  auth_proxy_hostname = local.auth_proxy_key != null ? local.service_hostnames[local.auth_proxy_key] : null

  # Service identified as the Authorization Server. At most one. Required
  # when var.jwt_verification.enabled (see eks-validate.tf precondition).
  auth_server_key      = one([for k, v in var.services : k if v.is_auth_server])
  auth_server_hostname = local.auth_server_key != null ? local.service_hostnames[local.auth_server_key] : null

  # MCP discovery manifest served by the Authorization Server in deployment.
  # The on-disk mcps/ scan only exists in a dev checkout, so deployed discovery
  # reads this rendered manifest (mounted as a file, see modules/service +
  # the chart's discovery ConfigMap). Includes the enabled MCP services only
  # (excludes the AS and the auth proxy). name/display_name use hostname_prefix
  # so they match the local mcps/<name>/mcp.json identifiers bond-ai expects.
  discovery_mcps = [
    for k, v in local.enabled_services : {
      name         = v.hostname_prefix
      display_name = v.hostname_prefix
      url          = "https://${local.service_hostnames[k]}/mcp"
    }
    if !v.is_auth_server && !v.is_auth_proxy
  ]
  discovery_json = jsonencode({ mcps = local.discovery_mcps })

  # ============================================================
  # Secrets Manager naming
  # ============================================================
  sm_prefix         = "${local.name_prefix}-"
  sm_encryption_key = "${local.sm_prefix}encryption-key"
  sm_db_credentials = "${local.sm_prefix}db-credentials"
  sm_jwt_public_key = "${local.sm_prefix}jwt-public-key"
  sm_as_credentials = "${local.sm_prefix}as-credentials"

  # ============================================================
  # Per-service env composition
  #
  # We auto-layer system-driven config on top of operator-provided
  # ``extra_env`` so an operator never has to repeat the same strings
  # (BOND_MCPS_AS_BASE_URL, BOND_MCPS_JWT_JWKS_URI, etc) per service.
  # Operator-set keys win over auto-layered defaults via merge order.
  # ============================================================

  as_base_url_resolved = (
    var.jwt_verification.as_base_url != ""
    ? var.jwt_verification.as_base_url
    : (local.auth_server_hostname != null ? "https://${local.auth_server_hostname}" : "")
  )
  jwks_uri_resolved = (
    var.jwt_verification.jwks_uri != ""
    ? var.jwt_verification.jwks_uri
    : (local.auth_server_hostname != null ? "https://${local.auth_server_hostname}/.well-known/jwks.json" : "")
  )

  # Env injected for the Authorization Server pod. Operator-set extra_env
  # is merged on top (and wins on conflict) — see services.tf.
  as_env = local.auth_server_key == null ? {} : merge(
    {
      BOND_MCPS_AS_ENABLED  = "1"
      BOND_MCPS_AS_BASE_URL = local.as_base_url_resolved
      # Path-only env — the file is mounted by helm if you choose that route,
      # but for k8s the SM secret injection of BOND_MCPS_AS_PRIVATE_KEY_PEM
      # is the supported path. See docs/deployment/oauth-resource-server.md.
      BOND_MCPS_UPSTREAM_IDP                = var.jwt_verification.upstream_idp
      BOND_MCPS_UPSTREAM_ISSUER             = var.jwt_verification.upstream_issuer
      BOND_MCPS_UPSTREAM_CLIENT_ID          = var.jwt_verification.upstream_client_id
      BOND_MCPS_UPSTREAM_REDIRECT_URI       = var.jwt_verification.upstream_redirect_uri
      BOND_MCPS_UPSTREAM_SCOPES             = var.jwt_verification.upstream_scopes
      BOND_MCPS_UPSTREAM_ALLOWED_DOMAINS    = var.jwt_verification.upstream_allowed_domains
      BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS   = var.jwt_verification.as_allowed_redirect_hosts
      BOND_MCPS_AS_ACCESS_TOKEN_TTL_SECONDS = tostring(var.jwt_verification.access_token_ttl_seconds)
    },
    var.services[local.auth_server_key].extra_env,
  )

  # Per-MCP env (every enabled non-AS, non-auth-proxy service that has
  # is_auth_server=false). MCPs auto-pick the issuer/jwks_uri/as_base_url so
  # operators don't repeat them; audience defaults to <public_url>/mcp at
  # runtime via auth.jwt_identity._resolve_audience.
  issuer_resolved = (
    var.jwt_verification.issuer != ""
    ? var.jwt_verification.issuer
    : local.as_base_url_resolved
  )

  mcp_env_overlay = !var.jwt_verification.enabled ? {} : {
    BOND_MCPS_JWT_JWKS_URI = local.jwks_uri_resolved
    BOND_MCPS_JWT_ISSUER   = local.issuer_resolved
    BOND_MCPS_AS_BASE_URL  = local.as_base_url_resolved
    # Disable FastMCP's per-pod session state. With >1 replica behind an ALB,
    # round-robin breaks the stateful protocol: pod A creates an Mcp-Session-Id
    # on /mcp `initialize`, then pod B 404s the next request with that ID
    # because it doesn't know about it. Stateless mode makes each request
    # self-contained; safe for our usage (no server-pushed events).
    FASTMCP_STATELESS_HTTP = "1"
  }
}
