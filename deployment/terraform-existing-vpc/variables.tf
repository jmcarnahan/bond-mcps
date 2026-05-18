# =========================================================================
# Identity / region
# =========================================================================

variable "project_name" {
  type        = string
  default     = "bond-mcps"
  description = "Prefix for all resource names. Should stay 'bond-mcps' for production."
}

variable "environment" {
  type        = string
  description = "Environment short name (dev, staging, prod). Goes into resource names and tags."
}

variable "aws_region" {
  type        = string
  default     = "us-west-2"
  description = "AWS region for every resource. Must match the region the existing VPC lives in."
}

# =========================================================================
# Existing VPC reuse
# =========================================================================

variable "existing_vpc_id" {
  type        = string
  description = "ID of the pre-existing VPC to deploy into (shared with bond-ai)."
}

variable "private_subnet_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Private subnets for Aurora and (in 3b) EKS nodes. At least 2, in different
    AZs, are required for Aurora HA. If empty, auto-discovered from existing_vpc_id
    via the aws_subnets data source filtered on map-public-ip-on-launch=false.
  EOT
}

# =========================================================================
# DNS + TLS
# =========================================================================

variable "base_domain" {
  type        = string
  description = <<-EOT
    Subdomain under which every service is exposed.
    Each service hostname becomes "<hostname_prefix>.<base_domain>".
    Example: "mcps.ai.southbayequity.cloud" → "auth.mcps.ai.southbayequity.cloud".
  EOT
}

variable "hosted_zone_id" {
  type        = string
  description = "Route53 hosted zone ID that owns base_domain. Used for ACM cert DNS validation and (in 3b) per-service ALIAS records."
}

# =========================================================================
# Services
# =========================================================================

variable "services" {
  description = <<-EOT
    Map of services to deploy. Keys are short names (e.g. "auth", "github").
    The map drives ECR repo creation, per-service SM secret shells, and (in 3b)
    Helm release instantiation. See deployment/helm/mcp-service/values.yaml for
    how the per-service fields map into chart values.
  EOT
  type = map(object({
    enabled            = bool
    image_repo_name    = string
    image_tag          = string
    container_port     = optional(number, 8000)
    hostname_prefix    = string
    replicas           = optional(number, 1)
    is_auth_proxy      = optional(bool, false)
    runs_migrations    = optional(bool, false)
    needs_scaling_work = optional(bool, false)
    oauth_secret_name  = optional(string)
    extra_env          = optional(map(string), {})
    health = optional(object({
      type = string
      path = optional(string, "/health")
    }), { type = "tcp" })
    resources = optional(object({
      requests = object({ cpu = string, memory = string })
      limits   = object({ cpu = string, memory = string })
    }))
  }))

  validation {
    condition     = length([for k, v in var.services : k if v.is_auth_proxy]) == 1
    error_message = "Exactly one service must have is_auth_proxy = true."
  }
}

# =========================================================================
# Aurora Postgres Serverless v2
# =========================================================================

variable "aurora_min_capacity" {
  type        = number
  default     = 1.0
  description = <<-EOT
    Aurora Serverless v2 minimum ACU.

    1.0 is the safe default: ~113 max connections, comfortable headroom
    for 9 worker processes × 5 conns each (pool_size 3 + max_overflow 2)
    plus preflight Jobs. Always-on cost ~$90/mo.

    0.5 is acceptable for dev (~56 max conns, $45/mo), where peak load
    rarely hits the budget. Override in environments/<env>.tfvars when
    appropriate. See README "Aurora connection budget".
  EOT
}

variable "aurora_max_capacity" {
  type        = number
  default     = 2
  description = "Aurora Serverless v2 maximum ACU."
}

variable "aurora_deletion_protection" {
  type        = bool
  default     = true
  description = "If true, deletion_protection on the Aurora cluster and a final snapshot are taken on destroy."
}

variable "ecr_force_delete" {
  type        = bool
  default     = false
  description = <<-EOT
    Force-delete ECR repositories on `terraform destroy` even if they
    contain images. Default false (matches AWS default; destroy fails
    until you empty the repo). Set true in dev so iterate-destroy cycles
    are cheap. Decoupled from aurora_deletion_protection so prod-Aurora /
    dev-ECR is a coherent combination.
  EOT
}

variable "ingress_default_scheme" {
  type        = string
  default     = "internet-facing"
  description = <<-EOT
    Default scheme for service ingresses. "internet-facing" exposes pods
    via a public ALB; "internal" stays inside the VPC. In non-dev
    environments, internet-facing requires var.jwt_verification.enabled
    = true — caught by the deploy-invariants precondition in
    eks-validate.tf.
  EOT

  validation {
    condition     = contains(["internet-facing", "internal"], var.ingress_default_scheme)
    error_message = "ingress_default_scheme must be \"internet-facing\" or \"internal\"."
  }
}

variable "aurora_engine_version" {
  type        = string
  default     = "15.12"
  description = "Aurora Postgres engine version (major.minor)."
}

# =========================================================================
# Multi-tenant identity (JWT verification)
# =========================================================================

variable "jwt_verification" {
  description = <<-EOT
    Multi-tenant identity via JWT verification. Off by default — every caller
    shares the chart's userKey value (single-tenant). When enabled, every
    HTTP request that reaches a Path-2 token-DB operation must carry
    X-Bond-Auth: Bearer <jwt>; the JWT is verified against the operator-
    supplied public key (seeded into the SM secret created by this module)
    and the sub claim becomes the request's user_key. See plan section H/C2.
  EOT
  type = object({
    enabled   = bool
    issuer    = optional(string, "")
    audience  = optional(string, "")
    algorithm = optional(string, "RS256")
    sub_claim = optional(string, "sub")
  })
  default = {
    enabled = false
  }
}

# =========================================================================
# Secrets Manager
# =========================================================================

variable "secrets_recovery_window_days" {
  type        = number
  default     = 0
  description = "AWS Secrets Manager recovery window. 0 = immediate deletion (dev). Use 7+ for prod."

  validation {
    condition     = var.secrets_recovery_window_days == 0 || (var.secrets_recovery_window_days >= 7 && var.secrets_recovery_window_days <= 30)
    error_message = "secrets_recovery_window_days must be 0, or between 7 and 30 (AWS-enforced range)."
  }
}

# =========================================================================
# EKS (consumed in 3b; declared here so dev.tfvars is the single source)
# =========================================================================

variable "eks_kubernetes_version" {
  type        = string
  default     = "1.31"
  description = "EKS control-plane version."
}

variable "eks_node_instance_type" {
  type        = string
  default     = "t3.medium"
  description = "Managed node group instance type."
}

variable "eks_node_min_count" {
  type    = number
  default = 1
}

variable "eks_node_desired_count" {
  type    = number
  default = 2
}

variable "eks_node_max_count" {
  type    = number
  default = 3
}

variable "eks_cluster_endpoint_public_access_cidrs" {
  type        = list(string)
  default     = ["0.0.0.0/0"]
  description = "CIDRs allowed to hit the EKS public API endpoint. Tighten in prod."
}
