output "service_key" {
  value = var.service_key
}

output "hostname" {
  value = var.hostname
}

output "url" {
  value = "https://${var.hostname}"
}

output "internal_dns" {
  value = "${var.service_key}.${var.namespace}.svc.cluster.local:${var.container_port}"
}
