variable "cluster_name" {
  description = "Name of the local kind cluster."
  type        = string
  default     = "self-healing-iam"
}

variable "namespace" {
  description = "Kubernetes namespace the ping-devops helm chart will be installed into."
  type        = string
  default     = "pingfederate"
}

variable "pingfederate_license_name" {
  description = "Name of the k8s secret holding the mounted PingFederate license file."
  type        = string
  default     = "pingfederate-license"
}

variable "pingfederate_license_path" {
  description = "Absolute local path to your PingFederate .lic file. Pass via TF_VAR_pingfederate_license_path — never commit the .lic file itself."
  type        = string
  default     = ""
}

variable "admin_console_port" {
  description = "Host port the PingFederate admin console is reachable at (https://localhost:<this>)."
  type        = number
  default     = 9999
}

variable "admin_console_node_port" {
  description = "NodePort (must be in 30000-32767) the admin_console_port is forwarded to inside the kind node."
  type        = number
  default     = 30999
}
