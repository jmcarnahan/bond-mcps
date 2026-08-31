# One module.service instantiation per enabled service. The chart consumes
# every value via yamlencode (see modules/service/main.tf), so per-service
# inputs flow cleanly through TF without `helm_release` dot-escape pain.
#
# user_key today is a deterministic per-service string. That keeps the deploy
# single-tenant — every API call from this pod reads/writes one token DB row.
# For multi-tenant operation, MCPs must derive user_key from the incoming
# Bearer JWT (plan section H C2).

module "service" {
  for_each = local.enabled_services
  source   = "./modules/service"

  service_key     = each.key
  environment_tag = var.environment
  ingress_scheme  = var.ingress_default_scheme
  ingress_subnets = var.public_subnet_ids
  vpc_cidr        = data.aws_vpc.existing.cidr_block
  namespace       = kubernetes_namespace.bond_mcps.metadata[0].name

  image_repository = aws_ecr_repository.this[each.key].repository_url
  image_tag        = local.effective_image_tag[each.key]
  container_port   = each.value.container_port
  replicas         = each.value.replicas
  is_auth_proxy    = each.value.is_auth_proxy
  is_auth_server   = each.value.is_auth_server
  runs_migrations  = each.value.runs_migrations
  command          = each.value.command

  user_key = "${local.name_prefix}-${each.key}"

  hostname           = local.service_hostnames[each.key]
  ingress_group_name = var.ingress_group_name

  # IRSA for the Authorization Server: annotate its ServiceAccount with the
  # role that grants cognito-idp:ListUsers (token-exchange email→sub lookup).
  # Only the AS pod gets it; MCP pods keep an un-annotated SA.
  service_account_annotations = (
    each.value.is_auth_server && local.enable_as_cognito_irsa
    ? { "eks.amazonaws.com/role-arn" = module.as_cognito_irsa[0].iam_role_arn }
    : {}
  )
  # Compose env in three layers: (1) operator-provided extra_env on the
  # service (highest precedence), (2) AS-only system env when this is the
  # AS, (3) MCP-only system env when this is a JWT-mode MCP. Layer order
  # in `merge` is "later wins" so the operator's extra_env is applied last
  # and overrides system defaults.
  extra_env = merge(
    each.value.is_auth_server ? local.as_env : (
      (var.jwt_verification.enabled && !each.value.is_auth_proxy) ? local.mcp_env_overlay : {}
    ),
    each.value.extra_env,
  )
  health    = each.value.health
  resources = each.value.resources

  auth_proxy_internal_host = local.auth_proxy_key != null ? "${local.auth_proxy_key}.${kubernetes_namespace.bond_mcps.metadata[0].name}.svc.cluster.local" : ""
  auth_proxy_port          = 8000
  auth_proxy_public_url    = local.auth_proxy_hostname != null ? "https://${local.auth_proxy_hostname}" : ""

  encryption_key_secret_name = aws_secretsmanager_secret.encryption_key.name
  # Services with their own logical database (db_secret_name) get their
  # per-service secret; everyone else shares the bondmcps master credentials.
  db_credentials_secret_name = (
    each.value.db_secret_name != null && each.value.db_secret_name != ""
    ? aws_secretsmanager_secret.service_db[each.key].name
    : aws_secretsmanager_secret.db_credentials.name
  )
  preflight_enabled = each.value.preflight_enabled
  # The AS automatically pulls its credentials from the SM secret terraform
  # creates for it (as-credentials). For MCPs, use the operator-specified
  # oauth_secret_name (the per-provider OAuth app credentials).
  oauth_secret_name = (
    each.value.is_auth_server
    ? local.sm_as_credentials
    : (each.value.oauth_secret_name != null && each.value.oauth_secret_name != ""
      ? "${local.sm_prefix}${each.value.oauth_secret_name}"
    : null)
  )

  cluster_secret_store_name = "bond-mcps-aws-sm"
  acm_certificate_arn       = aws_acm_certificate_validation.wildcard.certificate_arn

  # JWT verification config is only relevant for MCPs (and only when
  # jwt_verification.enabled). The AS doesn't verify JWTs — it issues them.
  jwt_enabled = var.jwt_verification.enabled && !each.value.is_auth_proxy && !each.value.is_auth_server
  jwt_secrets_manager_name = (
    local.enable_legacy_jwt_pk_secret ? aws_secretsmanager_secret.jwt_public_key[0].name : ""
  )
  jwt_jwks_uri    = local.jwks_uri_resolved
  jwt_issuer      = var.jwt_verification.issuer
  jwt_audience    = var.jwt_verification.audience
  jwt_algorithm   = var.jwt_verification.algorithm
  jwt_sub_claim   = var.jwt_verification.sub_claim
  jwt_as_base_url = local.as_base_url_resolved
  # Per-MCP public URL is the canonical hostname stitched together above.
  jwt_public_url = local.service_hostnames[each.key] != null ? "https://${local.service_hostnames[each.key]}" : ""

  # Deployment discovery manifest — only the Authorization Server serves
  # /connections/discovery in deployment, so only it gets the rendered file.
  discovery_json = each.value.is_auth_server ? local.discovery_json : ""

  depends_on = [
    kubectl_manifest.cluster_secret_store, # ESO must be ready
    helm_release.alb_controller,           # so the ingress class resolves
    aws_rds_cluster_instance.bond_mcps,    # so preflight can reach the DB
    terraform_data.encryption_key_seeded,  # so preflight has a real key
    # Every helm release waits for every image build. Module-level depends_on
    # is all-or-nothing, and here that is the behavior we want: if any image
    # fails to build, NO service rolls, so the cluster is never left half on
    # the new revision and half on the old.
    null_resource.build,
  ]
}
