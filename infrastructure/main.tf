provider "kind" {}

# Local kubernetes-in-docker cluster. No cloud account/CLI install needed —
# the tehcyx/kind provider embeds the kind library directly.
#
# extra_port_mappings forwards the host's admin_console_port straight into
# the node's NodePort range, so the PingFederate admin console is reachable
# at https://localhost:<admin_console_port> with no `kubectl port-forward`
# needed (see kubernetes_service.pingfederate_admin_nodeport below).
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
    }
  }
}

provider "kubernetes" {
  host                   = kind_cluster.this.endpoint
  cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  client_certificate     = kind_cluster.this.client_certificate
  client_key             = kind_cluster.this.client_key
}

resource "kubernetes_namespace" "this" {
  metadata {
    name = var.namespace
  }

  depends_on = [kind_cluster.this]
}

# Mounted into pingfederate-admin at
# /opt/in/instance/server/default/conf/pingfederate.lic (see
# pingfederate-values.yaml at repo root) so PingFederate starts with an
# existing license instead of trying to pull an evaluation one.
resource "kubernetes_secret" "pingfederate_license" {
  count = var.pingfederate_license_path != "" ? 1 : 0

  metadata {
    name      = var.pingfederate_license_name
    namespace = kubernetes_namespace.this.metadata[0].name
  }

  data = {
    "pingfederate.lic" = file(var.pingfederate_license_path)
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
