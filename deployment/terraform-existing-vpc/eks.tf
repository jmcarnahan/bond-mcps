# EKS cluster via terraform-aws-modules/eks/aws (same major as bond-ai).
# One managed node group, EKS-managed addons (vpc-cni, kube-proxy, coredns),
# envelope encryption with our eks KMS key, OIDC provider auto-created.

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  # The deploy-invariants gate is wired via aws_kms_key.eks (kms.tf) — module
  # depends_on cascades deferral into count{} inside the EKS module's
  # submodules and breaks plan. Doing it on the kms key (which module.eks
  # references in encryption_config) gates the cluster equivalently without
  # the cascade.

  # Cluster name is pinned by docs/PLATFORM-CONTRACT.md and decoupled from
  # local.name_prefix so a rename here does NOT churn the bond-mcps-<env>-*
  # Secrets Manager / ECR / IAM / Aurora resources.
  name               = var.cluster_name
  kubernetes_version = var.eks_kubernetes_version

  vpc_id     = data.aws_vpc.existing.id
  subnet_ids = local.private_subnet_ids

  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.eks_cluster_endpoint_public_access_cidrs

  enable_cluster_creator_admin_permissions = true

  encryption_config = {
    provider_key_arn = aws_kms_key.eks.arn
    resources        = ["secrets"]
  }

  enabled_log_types = ["api", "audit", "authenticator"]

  addons = {
    coredns = {}
    # vpc-cni and kube-proxy must install BEFORE the managed node group is
    # created: without aws-node/kube-proxy DaemonSets in place, the first
    # nodes come up with no CNI plugin, get stuck NotReady, and EKS times
    # out the node group with `NodeCreationFailure: Unhealthy nodes`. The
    # `before_compute = true` flag tells the module to roll these add-ons
    # in the same step as the cluster, so the manifests exist when nodes
    # register.
    kube-proxy             = { before_compute = true }
    vpc-cni                = { before_compute = true }
    eks-pod-identity-agent = {}
  }

  eks_managed_node_groups = {
    primary = {
      instance_types = [var.eks_node_instance_type]
      min_size       = var.eks_node_min_count
      desired_size   = var.eks_node_desired_count
      max_size       = var.eks_node_max_count

      labels = {
        role = "bond-mcps"
      }

      tags = {
        Name = "${local.name_prefix}-eks-node"
      }
    }
  }

  tags = { Name = var.cluster_name }
}

# Allow Postgres traffic from EKS nodes into the Aurora SG. Lives here (not
# in security.tf) because it references the EKS-module-created node SG —
# keeps the dependency arrow pointing one way.
resource "aws_vpc_security_group_ingress_rule" "aurora_from_eks_nodes" {
  security_group_id            = aws_security_group.aurora.id
  description                  = "Postgres from EKS node group"
  referenced_security_group_id = module.eks.node_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
