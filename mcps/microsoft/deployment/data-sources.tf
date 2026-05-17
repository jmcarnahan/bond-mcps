# Data sources for existing VPC resources

data "aws_caller_identity" "current" {}

data "aws_vpc" "existing" {
  id = var.existing_vpc_id
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.existing.id]
  }

  # Filter for private subnets (no public IP assignment)
  filter {
    name   = "map-public-ip-on-launch"
    values = ["false"]
  }
}

# -----------------------------------------------------------------------------
# VPC Endpoint Lookup (for private deployment)
# -----------------------------------------------------------------------------
# Looks up the existing apprunner.requests VPC endpoint created by the main
# deployment. Required when mcp_microsoft_is_private = true.

data "aws_vpc_endpoint" "apprunner_requests" {
  count        = var.mcp_microsoft_is_private && var.mcp_microsoft_enabled ? 1 : 0
  vpc_id       = data.aws_vpc.existing.id
  service_name = "com.amazonaws.${var.aws_region}.apprunner.requests"
  state        = "available"
}

locals {
  # Select subnets for App Runner VPC connector
  # Use explicitly provided subnet IDs if available, otherwise auto-detect
  app_runner_subnet_ids = length(var.app_runner_subnet_ids) > 0 ? var.app_runner_subnet_ids : slice(
    data.aws_subnets.private.ids,
    0,
    min(3, length(data.aws_subnets.private.ids))
  )

  # Private-aware service URL
  mcp_microsoft_url = (
    var.mcp_microsoft_enabled
    ? (var.mcp_microsoft_is_private
      ? aws_apprunner_vpc_ingress_connection.mcp_microsoft[0].domain_name
      : aws_apprunner_service.mcp_microsoft[0].service_url)
    : ""
  )
}
