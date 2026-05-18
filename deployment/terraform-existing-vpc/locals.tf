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
  # exactly one. one() returns null if the list is empty; here it never is.
  auth_proxy_key      = one([for k, v in var.services : k if v.is_auth_proxy])
  auth_proxy_hostname = local.auth_proxy_key != null ? local.service_hostnames[local.auth_proxy_key] : null

  # ============================================================
  # Secrets Manager naming
  # ============================================================
  sm_prefix         = "${local.name_prefix}-"
  sm_encryption_key = "${local.sm_prefix}encryption-key"
  sm_db_credentials = "${local.sm_prefix}db-credentials"
  sm_jwt_public_key = "${local.sm_prefix}jwt-public-key"
}
