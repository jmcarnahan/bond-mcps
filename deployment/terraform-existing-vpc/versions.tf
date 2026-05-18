terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
    null       = { source = "hashicorp/null", version = "~> 3.2" }
    tls        = { source = "hashicorp/tls", version = "~> 4.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.35" }
    helm       = { source = "hashicorp/helm", version = "~> 2.17" }
    kubectl    = { source = "gavinbunney/kubectl", version = "~> 1.19" }
    time       = { source = "hashicorp/time", version = "~> 0.12" }
  }
}
