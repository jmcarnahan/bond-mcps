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

variable "public_subnet_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Public subnets the shared ALB lives in. Stamped on every Ingress as
    alb.ingress.kubernetes.io/subnets so the AWS Load Balancer Controller
    doesn't need the kubernetes.io/role/elb auto-discovery tag (which
    requires modifying tags on the shared VPC's subnets). At least 2, in
    different AZs, are required for the ALB. When empty, the controller
    falls back to tag-based discovery.
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
    Example: "mcps.example.com" → "auth.mcps.example.com".
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
    is_auth_server     = optional(bool, false)
    runs_migrations    = optional(bool, false)
    needs_scaling_work = optional(bool, false)
    oauth_secret_name  = optional(string)
    # Override the container entrypoint. Used for the Authorization Server
    # which shares the auth image but runs ``python -m auth.auth_server``.
    # Empty list means use the image's default CMD.
    command   = optional(list(string), [])
    extra_env = optional(map(string), {})
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
    condition     = length([for k, v in var.services : k if v.is_auth_proxy]) <= 1
    error_message = "At most one service may have is_auth_proxy = true."
  }
  validation {
    condition     = length([for k, v in var.services : k if v.is_auth_server]) <= 1
    error_message = "At most one service may have is_auth_server = true."
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
    shares the chart's userKey value (single-tenant). When enabled, each MCP
    becomes an OAuth 2.1 Resource Server: FastMCP's RemoteAuthProvider
    mounts /.well-known/oauth-protected-resource and returns
    `401 + WWW-Authenticate: Bearer ... resource_metadata=...` for missing
    tokens. The standard `Authorization: Bearer <jwt>` is validated against
    `jwks_uri` (the bond-mcps AS's JWKS endpoint). The JWT's `sub` claim
    becomes the per-request user_key.

    When enabled, you must ALSO declare a service with is_auth_server = true
    in var.services AND populate the upstream_* fields below so the AS can
    sign in users against Cognito or Okta.

    See docs/deployment/oauth-resource-server.md.
  EOT
  type = object({
    enabled = bool
    # Required when enabled. JWKS URI for the bond-mcps AS — typically
    # "https://<auth-hostname>/.well-known/jwks.json".
    jwks_uri = optional(string, "")
    # Required when enabled. Issuer claim in JWTs and AS base URL.
    issuer = optional(string, "")
    # Optional friendly audience name in addition to the canonical PRM URI
    # (which the verifier auto-derives from BOND_MCPS_PUBLIC_URL/mcp). Leave
    # empty unless you have a non-Claude-Code caller that needs it.
    audience  = optional(string, "")
    algorithm = optional(string, "RS256")
    sub_claim = optional(string, "sub")
    # Same as issuer in the common case; surfaced separately so an
    # AS hosted at a different URL than the JWKS works.
    as_base_url = optional(string, "")
    # ---- Upstream OIDC IdP (consumed by the AS) ------------------------
    upstream_idp             = optional(string, "") # "cognito" or "okta"
    upstream_issuer          = optional(string, "") # OIDC issuer URL
    upstream_client_id       = optional(string, "") # AS's client ID at the IdP
    upstream_redirect_uri    = optional(string, "") # "<as_base_url>/oauth/upstream/callback"
    upstream_scopes          = optional(string, "openid email profile")
    upstream_allowed_domains = optional(string, "") # CSV; optional gate
    # CSV of hosts allowed as DCR redirect_uri (in addition to loopback).
    # Required for any Claude Code instance that uses an https loopback;
    # typical value is an org's workstation domain. Leave empty for loopback-only.
    as_allowed_redirect_hosts = optional(string, "")
    # Lengthen the AS-issued JWT TTL beyond the 24h default (in seconds).
    access_token_ttl_seconds = optional(number, 86400)
  })
  default = {
    enabled = false
  }

  validation {
    condition = !var.jwt_verification.enabled || (
      var.jwt_verification.upstream_idp != "" &&
      var.jwt_verification.upstream_issuer != "" &&
      var.jwt_verification.upstream_client_id != "" &&
      var.jwt_verification.upstream_redirect_uri != ""
    )
    error_message = "When jwt_verification.enabled=true, all of upstream_idp, upstream_issuer, upstream_client_id, and upstream_redirect_uri must be set. (jwks_uri and issuer are auto-derived from the is_auth_server service hostname when omitted.)"
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
