# Pre-deploy guardrails.
#
# This null_resource fails the apply if the encryption-key Secrets Manager
# value is still the placeholder string. Without this check, pods would
# come up and immediately crash-loop in their preflight initContainer with
# a confusing TokenEncryptionError — much harder to diagnose than a clean
# terraform-side failure.
#
# Runs every apply (triggers always changes). Cheap: one AWS call.

resource "null_resource" "encryption_key_seeded" {
  triggers = {
    # Always re-evaluate; the check itself is the source of truth.
    every_apply = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -e
      val=$(aws secretsmanager get-secret-value \
        --secret-id ${aws_secretsmanager_secret.encryption_key.name} \
        --query SecretString --output text \
        --region ${var.aws_region} 2>/dev/null || echo '{}')

      if echo "$val" | grep -q 'REPLACE_ME'; then
        cat >&2 <<MSG

ERROR: ${aws_secretsmanager_secret.encryption_key.name} still has the placeholder value.

Pods will crash in their preflight initContainer until you seed it:

  KEY=\$(cd ${path.cwd}/../../ && poetry --directory auth run bond-mcps generate-key)
  aws secretsmanager put-secret-value \\
    --secret-id ${aws_secretsmanager_secret.encryption_key.name} \\
    --region ${var.aws_region} \\
    --secret-string "{\"BOND_MCPS_ENCRYPTION_KEY\": \"\$KEY\"}"

Re-run \`terraform apply\` once seeded.

MSG
        exit 1
      fi
      echo "Encryption key looks seeded — proceeding."
    EOT
  }

  depends_on = [aws_secretsmanager_secret_version.encryption_key]
}
