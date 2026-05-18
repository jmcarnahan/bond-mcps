data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

data "aws_vpc" "existing" {
  id = var.existing_vpc_id
}

# All private subnets in the VPC. Used when var.private_subnet_ids is empty.
data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.existing.id]
  }
  filter {
    name   = "map-public-ip-on-launch"
    values = ["false"]
  }
}

data "aws_route53_zone" "main" {
  zone_id = var.hosted_zone_id
}
