# One ECR repository per service. Keys match var.services keys so CI can
# look up the URL by service name (output: ecr_repository_urls).

resource "aws_ecr_repository" "this" {
  for_each = var.services

  name                 = each.value.image_repo_name
  image_tag_mutability = "MUTABLE"
  force_delete         = var.ecr_force_delete

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.secrets.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = each.value.image_repo_name }
}

# Keep the last 20 tagged images. Old images sit around forever otherwise
# (ECR doesn't auto-clean by default) and accrue cost.
resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = var.services
  repository = aws_ecr_repository.this[each.key].name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}
