# Aurora security group. Ingress rules are added separately in 3b (eks.tf)
# referencing the EKS node SG, so the dependency arrow only points one way.

resource "aws_security_group" "aurora" {
  name        = "${local.name_prefix}-aurora"
  description = "Aurora Postgres ingress for bond-mcps"
  vpc_id      = data.aws_vpc.existing.id
  tags        = { Name = "${local.name_prefix}-aurora" }
}
