provider "kind" {}

# Local kubernetes-in-docker cluster. No cloud account/CLI install needed —
# the tehcyx/kind provider embeds the kind library directly.
#
# extra_port_mappings forwards the host's admin_console_port straight into
# the node's NodePort range, so the PingFederate admin console is reachable
# at https://localhost:<admin_console_port> with no `kubectl port-forward`
# needed (see kubernetes_service.pingfederate_admin_nodeport below).
#
# Single node: all kind nodes are containers on the same Docker Desktop VM
# and share its CPU/RAM regardless of node count, so extra worker nodes add
# per-node kubelet/kube-proxy/containerd overhead without adding capacity.
# Storage: no kubernetes_storage_class resource here — kind ships a default
# "standard" StorageClass backed by local-path-provisioner, sufficient for
# this local demo's PVCs.
resource "kind_cluster" "this" {
  name           = var.cluster_name
  wait_for_ready = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"

      extra_port_mappings {
        container_port = var.admin_console_node_port
        host_port      = var.admin_console_port
      }

      # Same forwarding trick as above, for Postgres — installed by
      # deploy-platform.sh.
      extra_port_mappings {
        container_port = var.postgres_node_port
        host_port      = var.postgres_port
      }
    }
  }
}

provider "kubernetes" {
  host                   = kind_cluster.this.endpoint
  cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  client_certificate     = kind_cluster.this.client_certificate
  client_key             = kind_cluster.this.client_key
}

# Shared by PingFederate and, in a later phase, Postgres — kept as one
# namespace rather than splitting workload vs. platform infra.
resource "kubernetes_namespace" "this" {
  metadata {
    name = var.namespace
  }

  depends_on = [kind_cluster.this]
}

# Mounted into pingfederate-admin/engine at
# /opt/in/instance/server/default/conf/pingfederate.lic (see
# helm/ping-devops/values.yaml) so PingFederate starts with an existing
# license instead of trying to pull an evaluation one. Same /opt/in mount
# pattern used below for the server profile (kubernetes_config_map.pingfederate_server_profile).
resource "kubernetes_secret" "pingfederate_license" {
  count = var.pingfederate_license_path != "" ? 1 : 0

  metadata {
    name      = var.pingfederate_license_name
    namespace = kubernetes_namespace.this.metadata[0].name
  }

  data = {
    "pingfederate.lic"       = file(var.pingfederate_license_path)
    "pf.jwk"                 = file(var.pingfederate_master_key_path)
    "PING_IDENTITY_PASSWORD" = var.pingfederate_admin_password
  }

  type = "Opaque"
}

# Exposes the admin console at https://localhost:<admin_console_port> via the
# kind extra_port_mappings above — no manual `kubectl port-forward` needed.
# Kept separate from the helm chart's own ClusterIP service so it survives
# `helm upgrade` untouched. Selector matches the pod labels the chart sets
# on the pingfederate-admin deployment.
resource "kubernetes_service" "pingfederate_admin_nodeport" {
  metadata {
    name      = "pingfederate-admin-nodeport"
    namespace = kubernetes_namespace.this.metadata[0].name
  }

  spec {
    type = "NodePort"

    selector = {
      "app.kubernetes.io/instance" = "pingfederate"
      "app.kubernetes.io/name"     = "pingfederate-admin"
    }

    port {
      port        = 9999
      target_port = 9999
      node_port   = var.admin_console_node_port
    }
  }
}

# Generated once and stored in a k8s Secret rather than a manifest file, so
# deploy-platform.sh never has a plaintext password committed anywhere —
# Postgres reads it via a mounted secret volume (helm/postgres.yaml).
resource "random_password" "postgres_admin" {
  length  = 24
  special = false
}

# Mounted into the postgres pod at /run/secrets/postgres-credentials
# (helm/postgres.yaml) — POSTGRES_PASSWORD_FILE points at the
# POSTGRES_JDBC_PASSWORD key so the official postgres image picks it up on
# first boot. Also read directly by pingfederate-admin/engine
# (helm/ping-devops/values.yaml's container.env/secretKeyRef).
resource "kubernetes_secret" "postgres_credentials" {
  metadata {
    name      = "postgres-credentials"
    namespace = kubernetes_namespace.this.metadata[0].name
  }

  data = {
    POSTGRES_JDBC_PASSWORD = random_password.postgres_admin.result
  }

  type = "Opaque"
}
