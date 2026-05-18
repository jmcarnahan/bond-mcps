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

  service_key = each.key
  namespace   = kubernetes_namespace.bond_mcps.metadata[0].name

  image_repository = aws_ecr_repository.this[each.key].repository_url
  image_tag        = each.value.image_tag
  container_port   = each.value.container_port
  replicas         = each.value.replicas
  is_auth_proxy    = each.value.is_auth_proxy
  runs_migrations  = each.value.runs_migrations

  user_key = "${local.name_prefix}-${each.key}"

  hostname  = local.service_hostnames[each.key]
  extra_env = each.value.extra_env
  health    = each.value.health
  resources = each.value.resources

  auth_proxy_internal_host = local.auth_proxy_key != null ? "${local.auth_proxy_key}.${kubernetes_namespace.bond_mcps.metadata[0].name}.svc.cluster.local" : ""
  auth_proxy_port          = 8000
  auth_proxy_public_url    = local.auth_proxy_hostname != null ? "https://${local.auth_proxy_hostname}" : ""

  encryption_key_secret_name = aws_secretsmanager_secret.encryption_key.name
  db_credentials_secret_name = aws_secretsmanager_secret.db_credentials.name
  oauth_secret_name = (
    each.value.oauth_secret_name != null && each.value.oauth_secret_name != ""
    ? "${local.sm_prefix}${each.value.oauth_secret_name}"
    : null
  )

  cluster_secret_store_name = "bond-mcps-aws-sm"
  acm_certificate_arn       = aws_acm_certificate_validation.wildcard.certificate_arn

  jwt_enabled              = var.jwt_verification.enabled
  jwt_secrets_manager_name = aws_secretsmanager_secret.jwt_public_key.name
  jwt_issuer               = var.jwt_verification.issuer
  jwt_audience             = var.jwt_verification.audience
  jwt_algorithm            = var.jwt_verification.algorithm
  jwt_sub_claim            = var.jwt_verification.sub_claim

  depends_on = [
    kubectl_manifest.cluster_secret_store, # ESO must be ready
    helm_release.alb_controller,           # so the ingress class resolves
    aws_rds_cluster_instance.bond_mcps,    # so preflight can reach the DB
    terraform_data.encryption_key_seeded,  # so preflight has a real key
    terraform_data.jwt_public_key_seeded,  # so JWT mode (when enabled) has a real PEM
  ]
}
