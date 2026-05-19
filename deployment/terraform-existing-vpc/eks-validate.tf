# -----------------------------------------------------------------------------
# Deploy-time invariants (env-correlated safety checks)
# -----------------------------------------------------------------------------
#
# Two preconditions evaluated at plan time:
#   1. Non-dev environments + internet-facing ingress must have JWT enabled.
#   2. Non-dev environments cannot leave EKS public API endpoint open to 0.0.0.0/0.
#
# Dev environments are exempt to keep iteration speed (open by default, no JWT).
# Wire this resource into module.eks via depends_on (see eks.tf) so even a
# phased `terraform apply -target=module.eks` runs the check first.

resource "terraform_data" "deploy_invariants" {
  lifecycle {
    precondition {
      condition = (
        var.environment == "dev" ||
        var.jwt_verification.enabled ||
        var.ingress_default_scheme == "internal"
      )
      error_message = <<-EOM
        Non-dev environments with `internet-facing` ingress must enable JWT
        verification — otherwise public endpoints accept unauthenticated
        Path-2 OAuth flows. Set EITHER:
          var.jwt_verification.enabled = true  (preferred)
          var.ingress_default_scheme   = "internal"
      EOM
    }

    precondition {
      condition = (
        var.environment == "dev" ||
        !contains(var.eks_cluster_endpoint_public_access_cidrs, "0.0.0.0/0")
      )
      error_message = <<-EOM
        Non-dev environments refuse 0.0.0.0/0 in
        var.eks_cluster_endpoint_public_access_cidrs.

        Restrict the EKS public API endpoint to your CI runner egress
        CIDR(s) and the operator bastion CIDR.
      EOM
    }
  }
}

# -----------------------------------------------------------------------------
# Secret-seeding guards
# -----------------------------------------------------------------------------
#
# We probe each guarded SM secret via `data.external`, which shells out to the
# AWS CLI and returns just a small JSON status object — never the secret
# material itself. That keeps the secret value out of terraform.tfstate, which
# the previous `data.aws_secretsmanager_secret_version` approach didn't.
#
# Status states:
#   missing     — secret has no version yet (first deploy, before the shell
#                 `aws_secretsmanager_secret_version` resource is applied)
#   placeholder — value still contains the literal "REPLACE_ME" / "REPLACE_WITH_PEM"
#   seeded      — operator has put-secret-value'd a real value
#
# `depends_on` defers the read until the secret_version exists in AWS;
# the precondition then fires at apply time and fails loudly if not seeded.
# Operators wire the result through services.tf depends_on (existing) so
# workloads never roll out with placeholder material.

# -----------------------------------------------------------------------------
# Encryption key
# -----------------------------------------------------------------------------

data "external" "encryption_key_check" {
  program = ["bash", "-c", <<-EOT
    set -euo pipefail

    aws_rc=0
    val=$(aws secretsmanager get-secret-value \
      --secret-id ${aws_secretsmanager_secret.encryption_key.name} \
      --region ${var.aws_region} \
      --query SecretString --output text 2>&1) || aws_rc=$?

    if [ "$aws_rc" -ne 0 ]; then
      if printf '%s' "$val" | grep -q "ResourceNotFoundException"; then
        printf '%s' '{"status":"missing"}'
        exit 0
      fi
      # Permission / network / other AWS error — surface loudly.
      echo "aws secretsmanager get-secret-value failed: $val" >&2
      exit 1
    fi

    if printf '%s' "$val" | grep -q "REPLACE_ME"; then
      printf '%s' '{"status":"placeholder"}'
    else
      printf '%s' '{"status":"seeded"}'
    fi
  EOT
  ]

  # Defer the read until the placeholder version has been applied to AWS;
  # otherwise first-apply would always see "missing" on bootstrap.
  depends_on = [aws_secretsmanager_secret_version.encryption_key]
}

resource "terraform_data" "encryption_key_seeded" {
  lifecycle {
    precondition {
      condition     = data.external.encryption_key_check.result.status == "seeded"
      error_message = <<-EOM
        ${aws_secretsmanager_secret.encryption_key.name} is not yet seeded
        (status: missing or placeholder).

        Seed the encryption key before applying:

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
# Legacy JWT public-key seeding check (only when jwt_verification.enabled AND
# jwks_uri is empty — meaning operator chose static-PEM mode).
# -----------------------------------------------------------------------------

data "external" "jwt_public_key_check" {
  count = local.enable_legacy_jwt_pk_secret ? 1 : 0

  program = ["bash", "-c", <<-EOT
    set -euo pipefail

    aws_rc=0
    val=$(aws secretsmanager get-secret-value \
      --secret-id ${aws_secretsmanager_secret.jwt_public_key[0].name} \
      --region ${var.aws_region} \
      --query SecretString --output text 2>&1) || aws_rc=$?

    if [ "$aws_rc" -ne 0 ]; then
      if printf '%s' "$val" | grep -q "ResourceNotFoundException"; then
        printf '%s' '{"status":"missing"}'
        exit 0
      fi
      echo "aws secretsmanager get-secret-value failed: $val" >&2
      exit 1
    fi

    if printf '%s' "$val" | grep -q "REPLACE_WITH_PEM"; then
      printf '%s' '{"status":"placeholder"}'
    else
      printf '%s' '{"status":"seeded"}'
    fi
  EOT
  ]

  depends_on = [aws_secretsmanager_secret_version.jwt_public_key]
}

resource "terraform_data" "jwt_public_key_seeded" {
  count = local.enable_legacy_jwt_pk_secret ? 1 : 0

  lifecycle {
    precondition {
      condition     = data.external.jwt_public_key_check[0].result.status == "seeded"
      error_message = <<-EOM
        ${local.sm_jwt_public_key} is not yet seeded
        (status: missing or placeholder) but legacy static-PEM JWT mode is
        active (jwt_verification.enabled = true AND jwks_uri is empty).

        Seed the PEM-encoded public key (Unix line endings; strip CR if
        seeded from Windows: `tr -d '\r' < cert.pem > cert.lf.pem`):

          jq -Rsn --rawfile pem /path/to/bond-ai-jwt-public.pem \
             '{BOND_MCPS_JWT_PUBLIC_KEY: $pem}' > /tmp/jwt-payload.json

          aws secretsmanager put-secret-value \
            --secret-id ${local.sm_jwt_public_key} \
            --region ${var.aws_region} \
            --secret-string file:///tmp/jwt-payload.json

          rm /tmp/jwt-payload.json

        Re-run `terraform apply` once seeded. Or set
        jwt_verification.jwks_uri to switch to the (preferred) JWKS path.
      EOM
    }
  }
}
