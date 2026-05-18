# Three customer-managed KMS keys. One per blast-radius boundary:
# - aurora:  encrypts Aurora storage + Performance Insights data
# - secrets: encrypts Secrets Manager secret material + ECR image layers
# - eks:     envelope-encrypts the cluster's etcd-resident k8s Secrets (used in 3b)
#
# Each key has rotation enabled and a 30-day deletion window. The default key
# policy (caller account = full admin) is sufficient — service principals
# get key access via the resource referencing them (RDS, SM, ECR, EKS).

resource "aws_kms_key" "aurora" {
  description             = "${local.name_prefix} Aurora Postgres storage encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = { Name = "${local.name_prefix}-aurora" }
}

resource "aws_kms_alias" "aurora" {
  name          = "alias/${local.name_prefix}-aurora"
  target_key_id = aws_kms_key.aurora.id
}

resource "aws_kms_key" "secrets" {
  description             = "${local.name_prefix} Secrets Manager + ECR encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = { Name = "${local.name_prefix}-secrets" }
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${local.name_prefix}-secrets"
  target_key_id = aws_kms_key.secrets.id
}

resource "aws_kms_key" "eks" {
  description             = "${local.name_prefix} EKS k8s Secrets envelope encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = { Name = "${local.name_prefix}-eks" }
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${local.name_prefix}-eks"
  target_key_id = aws_kms_key.eks.id
}
