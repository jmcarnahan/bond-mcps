# dev environment — shares VPC + hosted zone with bond-ai.

environment = "dev"

existing_vpc_id    = "vpc-REDACTED"
private_subnet_ids = ["subnet-REDACTED-priv-a", "subnet-REDACTED-priv-b"]

base_domain    = "mcps.ai.example.com"
hosted_zone_id = "Z-REDACTED-ZONE" # bond-ai's example.com zone

# Dev: skip deletion protection so iterate-destroy-iterate is cheap.
aurora_deletion_protection   = false
ecr_force_delete             = true # let `terraform destroy` clean up images
secrets_recovery_window_days = 0

# Dev: 0.5 ACU is enough for sparse load (~$45/mo cheaper than the
# 1.0 default). Production should leave aurora_min_capacity at default.
aurora_min_capacity = 0.5

services = {
  auth = {
    enabled            = true
    image_repo_name    = "bond-mcps-auth"
    image_tag          = "0.1.0"
    hostname_prefix    = "auth"
    replicas           = 1
    is_auth_proxy      = true
    runs_migrations    = true
    needs_scaling_work = true
    health             = { type = "http", path = "/health" }
  }

  microsoft = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-microsoft"
    image_tag         = "0.1.0"
    hostname_prefix   = "microsoft"
    replicas          = 2
    oauth_secret_name = "microsoft-oauth"
    extra_env         = { MS_TENANT_ID = "consumers" }
    health            = { type = "http", path = "/healthz" }
  }

  github = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-github"
    image_tag         = "0.1.0"
    hostname_prefix   = "github"
    replicas          = 2
    oauth_secret_name = "github-oauth"
    health            = { type = "http", path = "/healthz" }
  }

  atlassian = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-atlassian"
    image_tag         = "0.1.0"
    hostname_prefix   = "atlassian"
    replicas          = 2
    oauth_secret_name = "atlassian-oauth"
    health            = { type = "http", path = "/healthz" }
  }

  databricks = {
    enabled           = true
    image_repo_name   = "bond-mcps-mcp-databricks"
    image_tag         = "0.1.0"
    hostname_prefix   = "databricks"
    replicas          = 2
    oauth_secret_name = "databricks-oauth"
    health            = { type = "http", path = "/healthz" }
    extra_env = {
      DATABRICKS_HOST      = "https://CHANGE-ME.cloud.databricks.com"
      DATABRICKS_HTTP_PATH = "/sql/1.0/warehouses/CHANGE-ME"
    }
  }
}
