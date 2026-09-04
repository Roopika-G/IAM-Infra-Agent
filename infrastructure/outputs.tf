output "cluster_name" {
  value = kind_cluster.this.name
}

output "kubectl_context" {
  description = "Context name kind writes to your kubeconfig for this cluster."
  value       = "kind-${kind_cluster.this.name}"
}

output "namespace" {
  value = kubernetes_namespace.this.metadata[0].name
}

output "pingfederate_license_secret_name" {
  description = "Null when no license file was supplied (i.e. using devops user/key instead)."
  value       = try(kubernetes_secret.pingfederate_license[0].metadata[0].name, null)
}

output "admin_console_url" {
  value = "https://localhost:${var.admin_console_port}/pingfederate/app"
}

output "postgres_secret_name" {
  value = kubernetes_secret.postgres_credentials.metadata[0].name
}

output "postgres_port" {
  description = "Host port for psql/TablePlus/pgAdmin/etc. Connect as user 'postgres' with the password in the postgres-credentials secret."
  value       = var.postgres_port
}
