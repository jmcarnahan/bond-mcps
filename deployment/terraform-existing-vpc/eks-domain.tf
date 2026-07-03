# Route53 ALIAS records for every enabled service → shared ALB.
#
# The ALB is created lazily by the AWS Load Balancer Controller after our
# Ingress resources land in the cluster. Rather than a fixed-duration sleep
# (which races on cold accounts and bills the cache miss on every plan), we
# poll until exactly ONE ALB carries all three IngressGroup tags:
#
#   elbv2.k8s.aws/cluster = <cluster-name>
#   ingress.k8s.aws/stack = <var.ingress_group_name>   (the IngressGroup name)
#   bond-mcps-environment = <var.environment>
#
# The third tag is stamped via the chart's alb.ingress.kubernetes.io/tags
# annotation (see modules/service/main.tf ingress.environmentTag). Together
# the three tags are unique across stacks and across environments, so a
# stale or sibling ALB can't match.

resource "null_resource" "wait_for_alb" {
  triggers = {
    cluster_name = module.eks.cluster_name
    services_set = sha256(jsonencode(keys(local.enabled_services)))
    environment  = var.environment
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -e
      attempt=0
      max_attempts=60   # 60 * 5s = 5 min cap
      while [ $attempt -lt $max_attempts ]; do
        count=$(aws resourcegroupstaggingapi get-resources \
          --region ${var.aws_region} \
          --resource-type-filters elasticloadbalancing:loadbalancer \
          --tag-filters \
            "Key=elbv2.k8s.aws/cluster,Values=${module.eks.cluster_name}" \
            "Key=ingress.k8s.aws/stack,Values=${var.ingress_group_name}" \
            "Key=bond-mcps-environment,Values=${var.environment}" \
          --query 'length(ResourceTagMappingList)' \
          --output text 2>/dev/null || echo "0")

        if [ "$count" = "1" ]; then
          echo "ALB ready (1 match on ${var.ingress_group_name} + ${module.eks.cluster_name} + ${var.environment})."
          exit 0
        fi

        if [ "$count" -gt "1" ] 2>/dev/null; then
          echo "ERROR: $count ALBs match the ${var.ingress_group_name} IngressGroup tags. Expected 1." >&2
          echo "Inspect:  aws resourcegroupstaggingapi get-resources --region ${var.aws_region} --tag-filters Key=ingress.k8s.aws/stack,Values=${var.ingress_group_name}" >&2
          echo "Likely cause: a previous destroy left an orphan ALB. Delete it before re-applying." >&2
          exit 1
        fi

        attempt=$((attempt + 1))
        sleep 5
      done

      echo "ERROR: ALB not found within 5 minutes. The AWS Load Balancer Controller may be stuck." >&2
      echo "Inspect:  kubectl -n kube-system logs deploy/aws-load-balancer-controller" >&2
      exit 1
    EOT
  }

  depends_on = [module.service]
}

# Now safe to look up the ALB by its three-tag fingerprint.
data "aws_lb" "shared" {
  tags = {
    "elbv2.k8s.aws/cluster" = module.eks.cluster_name
    "ingress.k8s.aws/stack" = var.ingress_group_name
    "bond-mcps-environment" = var.environment
  }

  depends_on = [null_resource.wait_for_alb]
}

resource "aws_route53_record" "service" {
  for_each = local.enabled_services

  zone_id = var.hosted_zone_id
  name    = local.service_hostnames[each.key]
  type    = "A"

  alias {
    name                   = data.aws_lb.shared.dns_name
    zone_id                = data.aws_lb.shared.zone_id
    evaluate_target_health = true
  }
}
