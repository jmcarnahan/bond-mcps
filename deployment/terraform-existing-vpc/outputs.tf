# =========================================================================
# VPC
# =========================================================================

output "vpc_id" {
  value       = data.aws_vpc.existing.id
  description = "Reused VPC ID."
}

output "private_subnet_ids" {
  value       = local.private_subnet_ids
  description = "Private subnets used for Aurora and EKS nodes."
}

# =========================================================================
# Aurora
# =========================================================================

output "aurora_cluster_identifier" {
  value = aws_rds_cluster.bond_mcps.cluster_identifier
}

output "aurora_writer_endpoint" {
  value     = aws_rds_cluster.bond_mcps.endpoint
  sensitive = true
}

output "aurora_reader_endpoint" {
  value     = aws_rds_cluster.bond_mcps.reader_endpoint
  sensitive = true
}

output "aurora_port" {
  value = aws_rds_cluster.bond_mcps.port
}

# =========================================================================
# Secrets Manager
# =========================================================================

output "secrets_manager_secret_names" {
  description = "Every SM secret this module creates. Use these names with `aws secretsmanager put-secret-value` to seed real values."
  value = merge(
    {
      encryption_key = aws_secretsmanager_secret.encryption_key.name
      db_credentials = aws_secretsmanager_secret.db_credentials.name
    },
    length(aws_secretsmanager_secret.jwt_public_key) > 0
    ? { jwt_public_key = aws_secretsmanager_secret.jwt_public_key[0].name }
    : {},
    length(aws_secretsmanager_secret.as_credentials) > 0
    ? { as_credentials = aws_secretsmanager_secret.as_credentials[0].name }
    : {},
    { for k, s in aws_secretsmanager_secret.oauth : "${k}_oauth" => s.name },
  )
}

output "secrets_manager_secret_arns" {
  description = "ARNs of every SM secret. ESO IRSA is scoped to these."
  sensitive   = true
  value = concat(
    [aws_secretsmanager_secret.encryption_key.arn],
    [aws_secretsmanager_secret.db_credentials.arn],
    [for s in aws_secretsmanager_secret.jwt_public_key : s.arn],
    [for s in aws_secretsmanager_secret.as_credentials : s.arn],
    [for s in aws_secretsmanager_secret.oauth : s.arn],
  )
}

# =========================================================================
# ECR
# =========================================================================

output "ecr_repository_urls" {
  description = "Per-service ECR repository URLs. CI pushes here; chart pulls {url}:{image_tag}."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

# =========================================================================
# ACM
# =========================================================================

output "acm_certificate_arn" {
  description = "Wildcard cert for *.<base_domain>. Consumed by chart ingress."
  value       = aws_acm_certificate_validation.wildcard.certificate_arn
}

# =========================================================================
# EKS
# =========================================================================

output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_oidc_issuer_url" {
  value = module.eks.cluster_oidc_issuer_url
}

output "node_security_group_id" {
  description = "EKS node security group. bond-ai pins this value in its tfvars (eks_external_node_security_group_id) so co-located pods can reach bond-ai's Aurora — see docs/PLATFORM-CONTRACT.md."
  value       = module.eks.node_security_group_id
}

output "kubectl_config_cmd" {
  description = "One-liner to configure kubectl for this cluster."
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region}"
}

# =========================================================================
# ALB (resolved after first apply once the LB controller reconciles)
# =========================================================================

output "shared_alb_dns_name" {
  description = "DNS name of the shared ALB serving every service. Resolves after the LB controller creates the ALB (usually within 60s of first apply)."
  value       = data.aws_lb.shared.dns_name
}

output "shared_alb_group_name" {
  description = "alb.ingress.kubernetes.io/group.name shared by all platform ingresses (bond-mcps + bond-ai). See docs/PLATFORM-CONTRACT.md."
  value       = var.ingress_group_name
}

# =========================================================================
# Services
# =========================================================================

output "service_urls" {
  description = "Public URL of each enabled service."
  value       = { for k, m in module.service : k => m.url }
}

output "service_internal_dns" {
  description = "In-cluster DNS for service-to-service calls."
  value       = { for k, m in module.service : k => m.internal_dns }
}

output "auth_proxy_url" {
  description = "Public URL of the service flagged is_auth_proxy=true. Consumed by bond-ai's bond_mcp_config tfvar after deployment."
  value       = local.auth_proxy_key != null ? module.service[local.auth_proxy_key].url : null
}

# =========================================================================
# Service derivations + warnings (informational)
# =========================================================================

output "service_hostnames" {
  description = "Map of service short name → external hostname. Useful for register-with-provider workflows."
  value       = local.service_hostnames
}

output "needs_scaling_work_services" {
  description = "Services flagged needs_scaling_work — surfaced so operators see them after apply."
  value       = [for k, v in var.services : k if try(v.needs_scaling_work, false)]
}

# =========================================================================
# Image builds (build-stages.tf)
# =========================================================================

output "deployed_image_tags" {
  description = "Tag each service actually deploys: content-hash for repo-built images, hand-pinned for foreign ones (sbel)."
  value       = local.effective_image_tag
}

output "built_images" {
  description = "Images terraform builds during apply (enabled services only) and their content-hash tags."
  value       = { for bk in local.active_build_keys : bk => local.image_tags[bk] }
}
