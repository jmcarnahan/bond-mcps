# ============================================================================
# Microsoft Graph MCP Server - App Runner Service
# ============================================================================

# -----------------------------------------------------------------------------
# Security Group
# -----------------------------------------------------------------------------

resource "aws_security_group" "mcp_microsoft" {
  count = var.mcp_microsoft_enabled ? 1 : 0

  name_prefix = "${var.project_name}-${var.environment}-mcp-ms-"
  description = "Security group for Microsoft MCP App Runner VPC connector"
  vpc_id      = data.aws_vpc.existing.id

  # Allow all outbound traffic (HTTPS to graph.microsoft.com via NAT)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound"
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-ms-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# VPC Connector
# -----------------------------------------------------------------------------

resource "aws_apprunner_vpc_connector" "mcp_microsoft" {
  count = var.mcp_microsoft_enabled ? 1 : 0

  vpc_connector_name = "${var.project_name}-${var.environment}-mcp-ms-conn"
  subnets            = local.app_runner_subnet_ids
  security_groups    = [aws_security_group.mcp_microsoft[0].id]

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-ms-connector"
  }
}

# -----------------------------------------------------------------------------
# Auto Scaling Configuration
# -----------------------------------------------------------------------------

resource "aws_apprunner_auto_scaling_configuration_version" "mcp_microsoft" {
  count                           = var.mcp_microsoft_enabled ? 1 : 0
  auto_scaling_configuration_name = "${var.project_name}-${var.environment}-mcp-ms-as"

  min_size = var.mcp_microsoft_min_instances
  max_size = var.mcp_microsoft_max_instances

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-ms-as"
  }
}

# -----------------------------------------------------------------------------
# App Runner Service
# -----------------------------------------------------------------------------

resource "aws_apprunner_service" "mcp_microsoft" {
  count        = var.mcp_microsoft_enabled ? 1 : 0
  service_name = "${var.project_name}-${var.environment}-mcp-microsoft"

  source_configuration {
    authentication_configuration {
      access_role_arn = aws_iam_role.mcp_microsoft_ecr_access[0].arn
    }

    image_repository {
      image_identifier      = "${aws_ecr_repository.mcp_microsoft[0].repository_url}:latest"
      image_repository_type = "ECR"

      image_configuration {
        port = "8000"

        runtime_environment_variables = {
          PYTHONUNBUFFERED = "1"
        }
      }
    }

    auto_deployments_enabled = false
  }

  # Network configuration - Conditional ingress (public or private), VPC egress
  network_configuration {
    ingress_configuration {
      is_publicly_accessible = !var.mcp_microsoft_is_private
    }

    egress_configuration {
      egress_type       = "VPC"
      vpc_connector_arn = aws_apprunner_vpc_connector.mcp_microsoft[0].arn
    }
  }

  instance_configuration {
    cpu               = var.mcp_microsoft_cpu
    memory            = var.mcp_microsoft_memory
    instance_role_arn = aws_iam_role.mcp_microsoft_instance[0].arn
  }

  auto_scaling_configuration_arn = aws_apprunner_auto_scaling_configuration_version.mcp_microsoft[0].arn

  health_check_configuration {
    protocol            = "TCP"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 1
    unhealthy_threshold = 5
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-microsoft"
    Type = "MCP-Server"
  }

  depends_on = [
    null_resource.build_mcp_microsoft_image
  ]
}

# -----------------------------------------------------------------------------
# VPC Ingress Connection (Private Deployment)
# -----------------------------------------------------------------------------
# Links this private App Runner service to the shared VPC endpoint created by
# the main deployment. Provides a private domain name for VPC-internal access.

resource "aws_apprunner_vpc_ingress_connection" "mcp_microsoft" {
  count = var.mcp_microsoft_is_private && var.mcp_microsoft_enabled ? 1 : 0

  name        = "${var.project_name}-${var.environment}-mcp-ms-ingress"
  service_arn = aws_apprunner_service.mcp_microsoft[0].arn

  ingress_vpc_configuration {
    vpc_id          = data.aws_vpc.existing.id
    vpc_endpoint_id = data.aws_vpc_endpoint.apprunner_requests[0].id
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-ms-ingress"
  }
}
