# for_each over var.services where enabled=true. Each entry instantiates module.service,
# passing image, port, hostname, secrets refs, probes. depends_on the ESO ClusterSecretStore
# and Aurora cluster.
