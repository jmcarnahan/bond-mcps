# One bond-mcps service = one helm_release of the mcp-service chart.
# Values are assembled via yamlencode rather than dozens of `set` blocks —
# much cleaner for nested structures and avoids the dot-escape pain.
#
# nameOverride == release name ensures helpers produce fullname == release
# name (no "auth-mcp-service" double-up). That keeps the in-cluster Service
# DNS as "<service_key>.<namespace>.svc.cluster.local".

locals {
  default_resources = {
    requests = { cpu = "100m", memory = "256Mi" }
    limits   = { cpu = "1000m", memory = "1Gi" }
  }

  chart_values = {
    nameOverride = var.service_key

    image = {
      repository = var.image_repository
      tag        = var.image_tag
      pullPolicy = "IfNotPresent"
    }

    port     = var.container_port
    replicas = var.replicas
    env      = var.extra_env
    userKey  = var.user_key

    authProxy = {
      # The auth service itself doesn't need injection; everyone else does.
      inject    = var.is_auth_proxy ? false : true
      host      = var.auth_proxy_internal_host
      port      = var.auth_proxy_port
      publicUrl = var.auth_proxy_public_url
    }

    secrets = {
      encryptionKey = {
        enabled            = true
        secretsManagerName = var.encryption_key_secret_name
      }
      db = {
        enabled            = true
        secretsManagerName = var.db_credentials_secret_name
        sslmode            = "require"
      }
      oauth = {
        enabled            = var.oauth_secret_name != null && var.oauth_secret_name != ""
        secretsManagerName = var.oauth_secret_name != null ? var.oauth_secret_name : ""
      }
    }

    externalSecrets = {
      clusterSecretStoreName = var.cluster_secret_store_name
      refreshInterval        = "5m"
    }

    serviceAccount = {
      create      = true
      annotations = {}
    }

    ingress = {
      enabled        = true
      host           = var.hostname
      className      = "alb"
      certificateArn = var.acm_certificate_arn
      groupName      = "bond-mcps"
      # Stagger group.order so ALB rule eval is deterministic. Auth gets the
      # lowest number so its rule is evaluated first.
      groupOrder      = var.is_auth_proxy ? 1 : 10
      scheme          = "internet-facing"
      healthCheckPath = var.health.type == "http" ? var.health.path : "/"
      successCodes    = "200-499"
    }

    probes = {
      liveness = {
        type                = var.health.type
        path                = var.health.path
        initialDelaySeconds = 15
        periodSeconds       = 10
      }
      readiness = {
        type                = var.health.type
        path                = var.health.path
        initialDelaySeconds = 5
        periodSeconds       = 10
      }
    }

    migrations = {
      enabled = var.runs_migrations
      command = ["bond-mcps", "migrate-db"]
    }

    preflight = {
      enabled = true
      command = ["bond-mcps", "doctor"]
    }

    pdb = {
      enabled      = var.replicas > 1
      minAvailable = 1
    }

    resources = var.resources != null ? var.resources : local.default_resources
  }
}

resource "helm_release" "service" {
  name      = var.service_key
  chart     = "${path.module}/${var.chart_path}"
  namespace = var.namespace

  values = [yamlencode(local.chart_values)]

  wait    = true
  timeout = 600

  # The chart needs the cluster secret store + ALB controller + Aurora ready
  # before pods can complete their preflight. Service-side dependencies are
  # passed through from services.tf.
}
