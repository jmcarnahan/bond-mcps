# Secrets Manager. We create shells + an initial seeded version, but every
# secret_version has lifecycle { ignore_changes = [secret_string] } so values
# rotated outside TF (manual put-secret-value, SM managed rotation, etc.)
# don't bounce. This also keeps real secret material OUT of terraform.tfstate.
#
# Three classes:
#   1. encryption-key  : BOND_MCPS_ENCRYPTION_KEY. Seeded with a placeholder;
#                        operator runs `bond-mcps generate-key` then
#                        `aws secretsmanager put-secret-value`.
#   2. db-credentials  : Seeded with the cluster's real creds (random_password
#                        + endpoint/port/dbname). Usable as-is.
#   3. oauth (for_each): per-MCP OAuth client creds. Placeholder seed; operator
#                        registers OAuth app with the provider and runs
#                        put-secret-value with the client_id/client_secret.

# -------------------------------------------------------------------------
# Encryption key (BOND_MCPS_ENCRYPTION_KEY)
# -------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "encryption_key" {
  name                    = local.sm_encryption_key
  description             = "AES-256-GCM key for bond-mcps token DB encryption (BOND_MCPS_ENCRYPTION_KEY)"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.secrets_recovery_window_days

  tags = { Name = local.sm_encryption_key }
}

resource "aws_secretsmanager_secret_version" "encryption_key" {
  secret_id = aws_secretsmanager_secret.encryption_key.id
  secret_string = jsonencode({
    BOND_MCPS_ENCRYPTION_KEY = "REPLACE_ME_RUN_bond-mcps_generate-key"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# -------------------------------------------------------------------------
# Database credentials (assembled into BOND_MCPS_DB_URL by ESO template)
# -------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "db_credentials" {
  name                    = local.sm_db_credentials
  description             = "Aurora master credentials for bond-mcps"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.secrets_recovery_window_days

  tags = { Name = local.sm_db_credentials }
}

resource "aws_secretsmanager_secret_version" "db_credentials" {
  secret_id = aws_secretsmanager_secret.db_credentials.id
  secret_string = jsonencode({
    username = aws_rds_cluster.bond_mcps.master_username
    password = random_password.aurora_master.result
    host     = aws_rds_cluster.bond_mcps.endpoint
    port     = aws_rds_cluster.bond_mcps.port
    dbname   = aws_rds_cluster.bond_mcps.database_name
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# -------------------------------------------------------------------------
# JWT verification public key — LEGACY static-PEM mode only.
#
# Created ONLY when var.jwt_verification.enabled = true AND no AS service is
# declared (i.e. no service has is_auth_server=true). In that mode the
# operator ships the public key in SM instead of fetching it from the AS's
# /.well-known/jwks.json endpoint. When an AS service is declared, MCPs read
# the live JWKS from the AS and this secret is unnecessary; the AS's signing
# material lives in `as_credentials` instead.
# -------------------------------------------------------------------------

locals {
  enable_legacy_jwt_pk_secret = (
    var.jwt_verification.enabled && local.auth_server_key == null
  )
}

resource "aws_secretsmanager_secret" "jwt_public_key" {
  count                   = local.enable_legacy_jwt_pk_secret ? 1 : 0
  name                    = local.sm_jwt_public_key
  description             = "PEM-encoded public key for bond-mcps JWT identity verification (legacy static-PEM mode)"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.secrets_recovery_window_days

  tags = { Name = local.sm_jwt_public_key }
}

resource "aws_secretsmanager_secret_version" "jwt_public_key" {
  count     = local.enable_legacy_jwt_pk_secret ? 1 : 0
  secret_id = aws_secretsmanager_secret.jwt_public_key[0].id
  secret_string = jsonencode({
    BOND_MCPS_JWT_PUBLIC_KEY = "REPLACE_WITH_PEM_IF_JWT_VERIFICATION_ENABLED"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# -------------------------------------------------------------------------
# Authorization Server credentials (created when an AS service is declared).
#
# Holds the AS's RSA signing key (BOND_MCPS_AS_PRIVATE_KEY_PEM) plus the
# upstream IdP client_secret if the upstream client is confidential, and the
# HS256 shared secret used to verify bond-ai subject tokens during RFC 8693
# token exchange (BOND_MCPS_AS_BOND_JWT_SECRET ≡ bond-ai JWT_SECRET_KEY; see
# docs/PLATFORM-CONTRACT.md). The JSON keys are env var names — ESO
# dataFrom.extract maps each to a Kubernetes Secret key that the chart
# consumes via envFrom (the AS routes as-credentials through the chart's
# `oauth` ExternalSecret — services.tf oauth_secret_name).
#
# Real secret material is seeded manually post-apply via
# `aws secretsmanager put-secret-value`; the placeholders below only apply on
# first create and are pinned by lifecycle.ignore_changes so rotations don't
# bounce. Because ESO uses dataFrom.extract, once BOND_MCPS_AS_BOND_JWT_SECRET
# exists in the secret it flows to the AS pod env automatically — no chart
# change is needed (mirrors the optional BOND_MCPS_AS_PREVIOUS_KEY_PEM).
# -------------------------------------------------------------------------

locals {
  enable_as_credentials_secret = local.auth_server_key != null
}

resource "aws_secretsmanager_secret" "as_credentials" {
  count                   = local.enable_as_credentials_secret ? 1 : 0
  name                    = local.sm_as_credentials
  description             = "RSA signing key + upstream client_secret for the bond-mcps Authorization Server"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.secrets_recovery_window_days

  tags = { Name = local.sm_as_credentials }
}

resource "aws_secretsmanager_secret_version" "as_credentials" {
  count     = local.enable_as_credentials_secret ? 1 : 0
  secret_id = aws_secretsmanager_secret.as_credentials[0].id
  secret_string = jsonencode({
    BOND_MCPS_AS_PRIVATE_KEY_PEM     = "REPLACE_WITH_RSA_PEM"
    BOND_MCPS_UPSTREAM_CLIENT_SECRET = "" # Empty for public OIDC clients (PKCE-only)
    BOND_MCPS_AS_BOND_JWT_SECRET     = "REPLACE_WITH_BOND_AI_JWT_SECRET_KEY"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# -------------------------------------------------------------------------
# Per-MCP OAuth secrets (one per service that declared oauth_secret_name)
# -------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "oauth" {
  for_each = local.oauth_services

  name                    = "${local.sm_prefix}${each.value.oauth_secret_name}"
  description             = "OAuth credentials for bond-mcps ${each.key} service"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.secrets_recovery_window_days

  tags = { Name = "${local.sm_prefix}${each.value.oauth_secret_name}" }
}

resource "aws_secretsmanager_secret_version" "oauth" {
  for_each = aws_secretsmanager_secret.oauth

  secret_id = each.value.id
  secret_string = jsonencode({
    PLACEHOLDER = "REPLACE_VIA_aws_secretsmanager_put-secret-value"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
