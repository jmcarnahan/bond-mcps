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
