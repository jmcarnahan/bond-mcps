# IRSA roles for cluster addons.
#
# - ALB Load Balancer Controller: uses the IAM submodule's pre-baked policy
#   (terraform-aws-modules/iam keeps it in sync with AWS's published JSON).
# - External Secrets Operator: custom policy scoped to bond-mcps-${env}-*
#   secrets in this account/region, plus KMS decrypt limited to our
#   secrets-encryption key via the kms:ViaService condition.

# =============================================================================
# AWS Load Balancer Controller
# =============================================================================

module "alb_controller_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.50"

  role_name                              = "${local.name_prefix}-alb-controller"
  attach_load_balancer_controller_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }

  tags = { Name = "${local.name_prefix}-alb-controller" }
}

# =============================================================================
# External Secrets Operator
# =============================================================================

data "aws_iam_policy_document" "external_secrets" {
  # Read every bond-mcps-${env}-* secret in this account/region.
  statement {
    sid    = "ReadOurSecrets"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:ListSecretVersionIds",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.sm_prefix}*",
    ]
  }

  # Decrypt the SM-encrypted secret material via the dedicated KMS key.
  # kms:ViaService confines this permission to Secrets Manager calls only —
  # the role can't be borrowed to decrypt arbitrary KMS material.
  statement {
    sid     = "DecryptSecretMaterial"
    effect  = "Allow"
    actions = ["kms:Decrypt"]

    resources = [aws_kms_key.secrets.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "external_secrets" {
  name        = "${local.name_prefix}-external-secrets"
  description = "Read bond-mcps-${var.environment}-* SM secrets + decrypt via the secrets KMS key"
  policy      = data.aws_iam_policy_document.external_secrets.json
}

# IRSA role for the external-secrets ServiceAccount. Trust policy ties it to
# the OIDC provider issued by our EKS cluster and the specific SA name.
module "external_secrets_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.50"

  role_name = "${local.name_prefix}-external-secrets"

  role_policy_arns = {
    main = aws_iam_policy.external_secrets.arn
  }

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["external-secrets:external-secrets"]
    }
  }

  tags = { Name = "${local.name_prefix}-external-secrets" }
}
