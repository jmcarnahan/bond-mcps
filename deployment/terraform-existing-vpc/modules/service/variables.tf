variable "service_key" {
  type        = string
  description = "Short service name (auth, github, microsoft, ...). Used as Helm release name and Service DNS prefix."
}

variable "environment_tag" {
  type        = string
  description = "Environment short name. Stamped on the ALB as bond-mcps-environment=<value> so the IngressGroup ALB lookup is unique across stacks."
}

variable "ingress_scheme" {
  type        = string
  default     = "internet-facing"
  description = "ALB scheme — internet-facing or internal."
}

variable "namespace" {
  type        = string
  description = "k8s namespace to install into (typically bond-mcps)."
}

variable "image_repository" {
  type        = string
  description = "Full ECR repository URL."
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "container_port" {
  type    = number
  default = 8000
}

variable "replicas" {
  type    = number
  default = 1
}

variable "is_auth_proxy" {
  type    = bool
  default = false
}

variable "runs_migrations" {
  type    = bool
  default = false
}

variable "user_key" {
  type        = string
  description = "BOND_MCPS_USER_ID value. Single-tenant per pod today — see plan section H C2."
}

variable "hostname" {
  type        = string
  description = "External hostname this service is exposed at."
}

variable "extra_env" {
  type    = map(string)
  default = {}
}

variable "auth_proxy_internal_host" {
  type        = string
  description = "In-cluster DNS of the auth proxy service. Injected as AUTH_PROXY_HOST for non-auth services."
}

variable "auth_proxy_port" {
  type    = number
  default = 8000
}

variable "auth_proxy_public_url" {
  type        = string
  default     = ""
  description = "Public origin of the auth proxy (e.g. https://auth.mcps.example.com). Injected as BOND_AUTH_PROXY_PUBLIC_URL — auth/proxy_client.py builds OAuth redirect URIs off this."
}

variable "encryption_key_secret_name" {
  type        = string
  description = "Full Secrets Manager name (e.g. bond-mcps-dev-encryption-key) of the AES-256 key."
}

variable "db_credentials_secret_name" {
  type        = string
  description = "Full SM name of Aurora credentials. ESO templates BOND_MCPS_DB_URL from it."
}

variable "oauth_secret_name" {
  type        = string
  default     = null
  description = "Full SM name of this service's OAuth credentials. null = no OAuth secret."
}

variable "cluster_secret_store_name" {
  type    = string
  default = "bond-mcps-aws-sm"
}

variable "acm_certificate_arn" {
  type        = string
  description = "ACM cert ARN for the wildcard *.<base_domain> cert."
}

variable "health" {
  type = object({
    type = string
    path = optional(string, "/health")
  })
  default = { type = "tcp" }
}

variable "resources" {
  type = object({
    requests = object({ cpu = string, memory = string })
    limits   = object({ cpu = string, memory = string })
  })
  default = null
}

variable "jwt_enabled" {
  type    = bool
  default = false
}

variable "jwt_secrets_manager_name" {
  type        = string
  default     = ""
  description = "Full SM secret name holding {\"BOND_MCPS_JWT_PUBLIC_KEY\": \"<PEM>\"}. Required if jwt_enabled = true."
}

variable "jwt_issuer" {
  type    = string
  default = ""
}

variable "jwt_audience" {
  type    = string
  default = ""
}

variable "jwt_algorithm" {
  type    = string
  default = "RS256"
}

variable "jwt_sub_claim" {
  type    = string
  default = "sub"
}

variable "chart_path" {
  type        = string
  description = "Filesystem path to the mcp-service Helm chart (relative to this module)."
  default     = "../../../helm/mcp-service"
}
