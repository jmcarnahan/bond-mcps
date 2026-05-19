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
    # nameOverride must match the release name so the chart's `fullname`
    # helper collapses to a single token (otherwise it produces
    # `<release>-<nameOverride>` which can hit DNS-1123 limits *and* lets
    # underscores leak into k8s metadata names). The service_key may use
    # underscores (e.g. `auth_server`) — sanitize the same way the helm
    # release name is sanitized.
    nameOverride = replace(var.service_key, "_", "-")

    image = {
      repository = var.image_repository
      tag        = var.image_tag
      pullPolicy = "IfNotPresent"
    }

    port        = var.container_port
    replicas    = var.replicas
    isAuthProxy = var.is_auth_proxy
    command     = var.command
    env         = var.extra_env
    userKey     = var.user_key

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

    jwt = {
      enabled = var.jwt_enabled
      # Prefer JWKS URI when provided; otherwise fall through to the
      # static PEM/secret stored in Secrets Manager.
      jwksUri = var.jwt_jwks_uri
      publicKey = {
        secretsManagerName = var.jwt_secrets_manager_name
      }
      issuer    = var.jwt_issuer
      audience  = var.jwt_audience
      algorithm = var.jwt_algorithm
      subClaim  = var.jwt_sub_claim
      asBaseUrl = var.jwt_as_base_url
      publicUrl = var.jwt_public_url
    }

    networkPolicy = {
      # Off by default; flip to true in production tfvars + ensure vpcCidr
      # is set so ALB → pod traffic is permitted.
      enabled = false
      vpcCidr = var.vpc_cidr
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
      scheme          = var.ingress_scheme
      healthCheckPath = var.health.type == "http" ? var.health.path : "/"
      # 200-299 now that every service has a real /healthz returning 200.
      # Previously 200-499 to tolerate MCPs that lacked the route.
      successCodes   = "200-299"
      environmentTag = var.environment_tag
      # CSV passed to alb.ingress.kubernetes.io/subnets when the shared VPC
      # has no kubernetes.io/role/elb tags for auto-discovery.
      subnets = length(var.ingress_subnets) > 0 ? join(",", var.ingress_subnets) : ""
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
  # Helm release names must match [a-z0-9]([-a-z0-9]*[a-z0-9])? — no underscores
  # allowed. Service keys in var.services may contain underscores (e.g.
  # `auth_server`), so sanitize for the release name; everything else (chart
  # values, in-cluster DNS, hostnames) keeps the original key.
  name      = replace(var.service_key, "_", "-")
  chart     = "${path.module}/${var.chart_path}"
  namespace = var.namespace

  values = [yamlencode(local.chart_values)]

  wait    = true
  timeout = 600

  # The chart needs the cluster secret store + ALB controller + Aurora ready
  # before pods can complete their preflight. Service-side dependencies are
  # passed through from services.tf.
}
