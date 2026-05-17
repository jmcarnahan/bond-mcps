# ============================================================================
# GitHub MCP Server - Main Configuration
# ============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = var.project_name
      Component   = "mcp-github"
      ManagedBy   = "Terraform"
    }
  }
}

# -----------------------------------------------------------------------------
# ECR Repository
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "mcp_github" {
  count = var.mcp_github_enabled ? 1 : 0
  name  = "${var.project_name}-${var.environment}-mcp-github"

  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-github"
  }
}

# ECR Lifecycle Policy - keep last 5 images
resource "aws_ecr_lifecycle_policy" "mcp_github" {
  count      = var.mcp_github_enabled ? 1 : 0
  repository = aws_ecr_repository.mcp_github[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# ECR Repository Policy - allow App Runner to pull
resource "aws_ecr_repository_policy" "mcp_github" {
  count      = var.mcp_github_enabled ? 1 : 0
  repository = aws_ecr_repository.mcp_github[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAppRunnerPull"
        Effect = "Allow"
        Principal = {
          Service = "build.apprunner.amazonaws.com"
        }
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability"
        ]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# IAM Role: ECR Access (for App Runner to pull images)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "mcp_github_ecr_access" {
  count = var.mcp_github_enabled ? 1 : 0
  name  = "${var.project_name}-${var.environment}-mcp-gh-ecr-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "build.apprunner.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-gh-ecr-role"
  }
}

resource "aws_iam_role_policy_attachment" "mcp_github_ecr_access" {
  count      = var.mcp_github_enabled ? 1 : 0
  role       = aws_iam_role.mcp_github_ecr_access[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}

# -----------------------------------------------------------------------------
# IAM Role: Instance (for App Runner task execution)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "mcp_github_instance" {
  count = var.mcp_github_enabled ? 1 : 0
  name  = "${var.project_name}-${var.environment}-mcp-gh-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "tasks.apprunner.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-${var.environment}-mcp-gh-role"
  }
}

resource "aws_iam_role_policy" "mcp_github_instance" {
  count = var.mcp_github_enabled ? 1 : 0
  name  = "${var.project_name}-${var.environment}-mcp-gh-policy"
  role  = aws_iam_role.mcp_github_instance[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Docker Build and Push to ECR
# -----------------------------------------------------------------------------

resource "null_resource" "build_mcp_github_image" {
  count = var.mcp_github_enabled ? 1 : 0

  depends_on = [
    aws_ecr_repository.mcp_github,
    aws_ecr_repository_policy.mcp_github
  ]

  triggers = {
    force_rebuild   = var.force_rebuild
    dockerfile_hash = filemd5("${path.module}/../Dockerfile")
    pyproject_hash  = filemd5("${path.module}/../pyproject.toml")
    lock_hash       = filemd5("${path.module}/../poetry.lock")
    mcp_server_hash = filemd5("${path.module}/../github_mcp.py")

    # Hash of all Python files in github package
    github_hash = md5(join("", [
      for f in fileset("${path.module}/../github", "**/*.py") :
      filemd5("${path.module}/../github/${f}")
    ]))

    # Hash of shared_auth package (path dependency)
    shared_auth_hash = md5(join("", [
      for f in fileset("${path.module}/../../shared_auth/shared_auth", "**/*.py") :
      filemd5("${path.module}/../../shared_auth/shared_auth/${f}")
    ]))
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      echo "Building GitHub MCP server Docker image..."

      # Verify Docker is running
      if ! docker info > /dev/null 2>&1; then
        echo "Error: Docker daemon is not running"
        exit 1
      fi

      # Login to ECR
      ECR_REGISTRY="${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
      echo "Authenticating with ECR at $ECR_REGISTRY..."

      # Clear existing credentials to prevent Keychain conflicts on macOS
      if [[ "$OSTYPE" == "darwin"* ]]; then
        while security delete-internet-password -s "$ECR_REGISTRY" 2>/dev/null; do :; done
      fi

      aws ecr get-login-password --region ${var.aws_region} | \
        docker login --username AWS --password-stdin $ECR_REGISTRY

      # Copy shared_auth into build context (path dependency in pyproject.toml)
      rm -rf ./_shared_auth_pkg
      mkdir -p ./_shared_auth_pkg
      cp -r ../shared_auth/shared_auth ./_shared_auth_pkg/shared_auth
      cp ../shared_auth/pyproject.toml ./_shared_auth_pkg/pyproject.toml
      trap 'rm -rf ./_shared_auth_pkg' EXIT

      # Build and push
      echo "Building and pushing GitHub MCP image..."
      docker buildx build --platform linux/amd64 \
        -t ${aws_ecr_repository.mcp_github[0].repository_url}:latest \
        -f Dockerfile --push .

      # Verify image was pushed
      aws ecr describe-images \
        --repository-name ${aws_ecr_repository.mcp_github[0].name} \
        --region ${var.aws_region} \
        --image-ids imageTag=latest > /dev/null 2>&1

      if [ $? -eq 0 ]; then
        echo "GitHub MCP image built and pushed successfully"
      else
        echo "Error: Failed to verify GitHub MCP image in ECR"
        exit 1
      fi
    EOT

    working_dir = "${path.module}/.."
  }

  provisioner "local-exec" {
    when    = destroy
    command = "echo 'GitHub MCP Docker image will remain in ECR for potential reuse'"
  }
}
