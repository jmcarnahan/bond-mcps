# Template tfvars. Copy to environments/<env>.tfvars and fill the REQUIRED
# fields; all OPTIONAL fields have sensible defaults declared in variables.tf.

# ============================================================
# REQUIRED
# ============================================================

environment = "dev" # short env name, goes into resource names

existing_vpc_id = "vpc-xxxxxxxx"     # VPC ID to deploy into
base_domain     = "mcps.example.com" # services exposed as <prefix>.<base_domain>
hosted_zone_id  = "Zxxxxxxxxxxxx"    # Route53 zone owning base_domain

services = {
  auth = {
    enabled            = true
    image_repo_name    = "bond-mcps-auth"
    image_tag          = "0.1.0"
    hostname_prefix    = "auth"
    replicas           = 1
    is_auth_proxy      = true
    runs_migrations    = true
    needs_scaling_work = true # auth holds in-memory pending state; >1 replica breaks OAuth
    health             = { type = "http", path = "/health" }
  }

  github = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-github"
    image_tag         = "0.1.0"
    hostname_prefix   = "github"
    replicas          = 2
    oauth_secret_name = "github-oauth"
  }

  # microsoft = { …same shape, oauth_secret_name = "microsoft-oauth", extra_env = { MS_TENANT_ID = "consumers" } }
  # atlassian = { …same shape, oauth_secret_name = "atlassian-oauth" }
  # databricks = {
  #   …same shape, oauth_secret_name = "databricks-oauth",
  #   extra_env = {
  #     DATABRICKS_HOST      = "https://<workspace-id>.cloud.databricks.com"
  #     DATABRICKS_HTTP_PATH = "/sql/1.0/warehouses/<warehouse-id>"
  #   }
  # }
}

# ============================================================
# OPTIONAL (defaults in variables.tf)
# ============================================================

# aws_region                  = "us-west-2"
# private_subnet_ids          = []                # auto-discovers private subnets
# aurora_min_capacity         = 0.5
# aurora_max_capacity         = 2
# aurora_deletion_protection  = true
# secrets_recovery_window_days = 0                 # 0 = immediate (dev); use 7+ for prod
# eks_kubernetes_version       = "1.31"
# eks_node_instance_type       = "t3.medium"
# eks_node_min_count           = 1
# eks_node_desired_count       = 2
# eks_node_max_count           = 3
