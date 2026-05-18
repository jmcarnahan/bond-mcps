# External Secrets Operator + a single ClusterSecretStore pointing at AWS SM
# in this account/region. The chart's ExternalSecret resources reference the
# store by name ("bond-mcps-aws-sm") via .Values.externalSecrets.clusterSecretStoreName.

resource "helm_release" "external_secrets" {
  name       = "external-secrets"
  repository = "https://charts.external-secrets.io"
  chart      = "external-secrets"
  version    = "0.10.5"
  namespace  = kubernetes_namespace.external_secrets.metadata[0].name

  wait    = true
  timeout = 600

  set {
    name  = "installCRDs"
    value = "true"
  }
  set {
    name  = "serviceAccount.create"
    value = "true"
  }
  set {
    name  = "serviceAccount.name"
    value = "external-secrets"
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = module.external_secrets_irsa.iam_role_arn
  }

  depends_on = [
    kubernetes_namespace.external_secrets,
    module.external_secrets_irsa,
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
