# Cluster-scoped k8s objects: namespaces for our workloads and ESO.
#
# Namespaces depend on the cluster existing AND on the kubernetes provider
# being able to authenticate to it. The provider's exec auth handles that
# at use-time (no explicit data.aws_eks_cluster_auth needed).

resource "kubernetes_namespace" "bond_mcps" {
  metadata {
    name = "bond-mcps"
    labels = {
      "app.kubernetes.io/part-of" = "bond-mcps"
    }
  }

  depends_on = [module.eks]
}

resource "kubernetes_namespace" "external_secrets" {
  metadata {
    name = "external-secrets"
  }

  depends_on = [module.eks]
}
