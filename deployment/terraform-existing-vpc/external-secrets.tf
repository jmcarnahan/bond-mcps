# External Secrets Operator + a single ClusterSecretStore pointing at AWS SM
# in this account/region. The chart's ExternalSecret resources reference the
# store by name ("bond-mcps-aws-sm") via .Values.externalSecrets.clusterSecretStoreName.
#
# Helm values are passed as a yamlencode block (same pattern as
# modules/service/main.tf) so annotation keys with dots don't need
# backslash-escaping.

resource "helm_release" "external_secrets" {
  name       = "external-secrets"
  repository = "https://charts.external-secrets.io"
  chart      = "external-secrets"
  version    = "0.10.5"
  namespace  = kubernetes_namespace.external_secrets.metadata[0].name

  wait    = true
  timeout = 600

  values = [yamlencode({
    installCRDs = true
    serviceAccount = {
      create = true
      name   = "external-secrets"
      annotations = {
        "eks.amazonaws.com/role-arn" = module.external_secrets_irsa.iam_role_arn
      }
    }
  })]

  depends_on = [
    kubernetes_namespace.external_secrets,
    module.external_secrets_irsa,
    # The AWS Load Balancer Controller registers a MutatingWebhookConfiguration
    # that intercepts *every* Service creation cluster-wide. If ESO installs
    # in parallel, its chart's Service objects are rejected with
    # "no endpoints available for service aws-load-balancer-webhook-service"
    # because the LB controller pod hasn't finished starting yet. Serialize:
    # ALB controller helm release waits (wait=true) for the controller pod
    # to be Ready, so by the time we get here the webhook backend exists.
    helm_release.alb_controller,
  ]
}

# ClusterSecretStore — one per cluster, scoped to AWS SM in our region.
# kubectl_manifest avoids the kubernetes provider's strict CRD handling
# (which requires CRDs to exist at plan time).
resource "kubectl_manifest" "cluster_secret_store" {
  yaml_body = yamlencode({
    apiVersion = "external-secrets.io/v1beta1"
    kind       = "ClusterSecretStore"
    metadata = {
      name = "bond-mcps-aws-sm"
    }
    spec = {
      provider = {
        aws = {
          service = "SecretsManager"
          region  = var.aws_region
          auth = {
            jwt = {
              serviceAccountRef = {
                name      = "external-secrets"
                namespace = kubernetes_namespace.external_secrets.metadata[0].name
              }
            }
          }
        }
      }
    }
  })

  depends_on = [helm_release.external_secrets]
}
