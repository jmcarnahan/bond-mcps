# IRSA for the Authorization Server: cognito-idp:ListUsers on the user pool.
#
# During RFC 8693 token exchange the AS maps a bond-ai subject token's email to
# the caller's Cognito `sub` by calling ListUsers with an `email = "..."`
# filter (auth/auth/auth_server/cognito_lookup.py). The AS pod therefore needs
# AWS credentials scoped to exactly that one pool — delivered via IRSA on the
# auth_server ServiceAccount (annotation wired through modules/service →
# services.tf). MCP pods get no such role.
#
# Gated on a pool id being configured AND an AS service being declared, so
# non-JWT / dev-passthrough deployments create nothing here.

locals {
  enable_as_cognito_irsa = local.auth_server_key != null && var.cognito_user_pool_id != ""

  # ServiceAccount name the chart renders for the AS. The Helm release name
  # (and thus fullname/serviceAccountName) sanitizes underscores to dashes, so
  # `auth_server` → `auth-server` (see modules/service/main.tf).
  as_service_account_name = local.auth_server_key != null ? replace(local.auth_server_key, "_", "-") : ""

  # Pool region defaults to the stack region when cognito_region is unset.
  cognito_arn_region = var.cognito_region != "" ? var.cognito_region : var.aws_region

  cognito_user_pool_arn = "arn:${data.aws_partition.current.partition}:cognito-idp:${local.cognito_arn_region}:${data.aws_caller_identity.current.account_id}:userpool/${var.cognito_user_pool_id}"
}

data "aws_iam_policy_document" "as_cognito" {
  count = local.enable_as_cognito_irsa ? 1 : 0

  statement {
    sid       = "ListCognitoUsersForTokenExchange"
    effect    = "Allow"
    actions   = ["cognito-idp:ListUsers"]
    resources = [local.cognito_user_pool_arn]
  }
}

resource "aws_iam_policy" "as_cognito" {
  count       = local.enable_as_cognito_irsa ? 1 : 0
  name        = "${local.name_prefix}-as-cognito"
  description = "Allow the bond-mcps Authorization Server to ListUsers on the token-exchange Cognito pool"
  policy      = data.aws_iam_policy_document.as_cognito[0].json
}

module "as_cognito_irsa" {
  count   = local.enable_as_cognito_irsa ? 1 : 0
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.50"

  role_name = "${local.name_prefix}-as-cognito"

  role_policy_arns = {
    main = aws_iam_policy.as_cognito[0].arn
  }

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["${kubernetes_namespace.bond_mcps.metadata[0].name}:${local.as_service_account_name}"]
    }
  }

  tags = { Name = "${local.name_prefix}-as-cognito" }
}
