# Pre-deploy guardrails.
#
# Each `terraform_data` block reads the current SM secret value at plan time
# and asserts (via lifecycle.precondition) that it isn't the placeholder.
# This means `terraform plan` itself fails with a clear message — no
# local-exec, no per-apply AWS CLI shell-out, no path.cwd fragility.
#
# Two guards:
#   - encryption-key: always required
#   - JWT public key:  only when var.jwt_verification.enabled = true
#
# Both feed into module.service.depends_on so workloads never roll out with
# placeholder secret material that would crash the preflight initContainer.

# -----------------------------------------------------------------------------
# Encryption key
# -----------------------------------------------------------------------------

data "aws_secretsmanager_secret_version" "encryption_key_current" {
  secret_id = aws_secretsmanager_secret.encryption_key.id

  # Wait until our placeholder version exists so the lookup doesn't 404 on
  # very first apply. lifecycle.ignore_changes on the version means TF
  # won't churn after the operator seeds the real value.
  depends_on = [aws_secretsmanager_secret_version.encryption_key]
}

resource "terraform_data" "encryption_key_seeded" {
  input = data.aws_secretsmanager_secret_version.encryption_key_current.secret_string

  lifecycle {
    precondition {
      condition = !can(regex(
        "REPLACE_ME",
        data.aws_secretsmanager_secret_version.encryption_key_current.secret_string
      ))
      error_message = <<-EOM
        ${aws_secretsmanager_secret.encryption_key.name} still has the
        placeholder value. Seed it before applying:

          cd <repo>/auth
          KEY=$(poetry run bond-mcps generate-key)
          aws secretsmanager put-secret-value \
            --secret-id ${aws_secretsmanager_secret.encryption_key.name} \
            --region ${var.aws_region} \
            --secret-string "{\"BOND_MCPS_ENCRYPTION_KEY\": \"$KEY\"}"

        Re-run `terraform apply` once seeded.
      EOM
    }
  }
}

# -----------------------------------------------------------------------------
# JWT public key (only when multi-tenant identity is enabled)
# -----------------------------------------------------------------------------

data "aws_secretsmanager_secret_version" "jwt_public_key_current" {
  count = var.jwt_verification.enabled ? 1 : 0

  secret_id = aws_secretsmanager_secret.jwt_public_key.id

  depends_on = [aws_secretsmanager_secret_version.jwt_public_key]
}

resource "terraform_data" "jwt_public_key_seeded" {
  count = var.jwt_verification.enabled ? 1 : 0

  input = data.aws_secretsmanager_secret_version.jwt_public_key_current[0].secret_string

  lifecycle {
    precondition {
      condition = !can(regex(
        "REPLACE_WITH_PEM",
        data.aws_secretsmanager_secret_version.jwt_public_key_current[0].secret_string
      ))
      error_message = <<-EOM
        ${aws_secretsmanager_secret.jwt_public_key.name} still has the
        placeholder value but var.jwt_verification.enabled = true.

        Seed the PEM-encoded public key (PEM must use Unix line endings):

          jq -Rsn --rawfile pem /path/to/bond-ai-jwt-public.pem \
             '{BOND_MCPS_JWT_PUBLIC_KEY: $pem}' > /tmp/jwt-payload.json

          aws secretsmanager put-secret-value \
            --secret-id ${aws_secretsmanager_secret.jwt_public_key.name} \
            --region ${var.aws_region} \
            --secret-string file:///tmp/jwt-payload.json

          rm /tmp/jwt-payload.json

        Re-run `terraform apply` once seeded. Or flip
        var.jwt_verification.enabled = false to revert to single-tenant mode.
      EOM
    }
  }
}
