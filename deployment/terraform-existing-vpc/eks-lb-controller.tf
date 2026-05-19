# AWS Load Balancer Controller. Installed via the official chart.
# IRSA role from iam.tf is attached via the SA annotation in chart values.
#
# Values are passed as a single yamlencode block rather than dozens of
# `set` entries — avoids the dot-escape pitfall on annotation keys and
# matches the pattern used in modules/service/main.tf.

resource "helm_release" "alb_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "1.11.0"
  namespace  = "kube-system"

  wait    = true
  timeout = 600

  values = [yamlencode({
    clusterName = module.eks.cluster_name
    # EKS managed-node-group default hop-limit (1) blocks pods from
    # reaching IMDS. Pass region + vpcId explicitly so the controller never
    # falls back to instance-metadata discovery.
    region = var.aws_region
    vpcId  = data.aws_vpc.existing.id
    serviceAccount = {
      create = true
      name   = "aws-load-balancer-controller"
      annotations = {
        "eks.amazonaws.com/role-arn" = module.alb_controller_irsa.iam_role_arn
      }
    }
  })]

  depends_on = [
    module.eks,
    module.alb_controller_irsa,
  ]
}
