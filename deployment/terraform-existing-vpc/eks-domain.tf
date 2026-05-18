# Route53 ALIAS records for every enabled service → shared ALB.
#
# The ALB is created lazily by the AWS Load Balancer Controller after our
# Ingress resources land in the cluster. helm_release.service finishes once
# pods are Ready, but the ALB tag-stamped lookup may still race. The
# time_sleep window gives the controller enough time to reconcile.
#
# If your first apply errors on data.aws_lb.shared (no matching LB), the
# usual cause is the wait was too short. Re-run `terraform apply` — by then
# the ALB exists and the data source resolves.

resource "time_sleep" "wait_for_alb" {
  depends_on      = [module.service]
  create_duration = "90s"
}

data "aws_lb" "shared" {
  tags = {
    "elbv2.k8s.aws/cluster" = module.eks.cluster_name
    "ingress.k8s.aws/stack" = "bond-mcps" # IngressGroup name from chart
  }

  depends_on = [time_sleep.wait_for_alb]
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
