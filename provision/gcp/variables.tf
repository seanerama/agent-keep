# Agent Keep — GCP provisioner: the UNIFORM provisioner interface (ADR 0009).
# The shared variable names (ssh_public_key, instance_size, allowed_ssh_cidr,
# region, name) MATCH provision/aws/ EXACTLY, so the operator workflow (and any
# wrapper) is identical across clouds. Only the defaults that are genuinely
# cloud-shaped (region, instance size) differ, plus the GCP-specific `project`
# and `zone` that AWS has no analogue for.

variable "ssh_public_key" {
  description = <<-EOT
    The operator's EXISTING SSH PUBLIC key, registered on the instance so you can
    ssh in. Pass EITHER the key material ("ssh-ed25519 AAAA...") OR a path to a
    .pub file ("~/.ssh/id_ed25519.pub"). This is a PUBLIC key only — NEVER a
    private key, and never a secret. It is stored in Terraform state (gitignored),
    not in git.
  EOT
  type        = string

  validation {
    # A public key starts with a key-type token; a private key ("-----BEGIN ...")
    # or an empty value is rejected outright. (A file path is resolved in main.tf
    # via file(); this guards the inline-material case.)
    condition     = length(trimspace(var.ssh_public_key)) > 0 && !startswith(trimspace(var.ssh_public_key), "-----BEGIN")
    error_message = "ssh_public_key must be a PUBLIC key (material or a .pub path), never a private key."
  }
}

variable "instance_size" {
  description = "GCE machine type. MUST be x86_64/amd64 (ADR 0009 — chassis images are amd64, not t2a/arm64). Default e2-small."
  type        = string
  default     = "e2-small"
}

variable "allowed_ssh_cidr" {
  description = <<-EOT
    CIDR allowed to reach SSH (port 22). REQUIRED — no default — set it to YOUR
    public IP as a /32 (e.g. "203.0.113.4/32"). NEVER 0.0.0.0/0. The dev-http
    ports (8377/8477) are NOT opened publicly; reach them via an SSH tunnel.
  EOT
  type        = string

  validation {
    condition     = var.allowed_ssh_cidr != "0.0.0.0/0"
    error_message = "allowed_ssh_cidr must not be 0.0.0.0/0 (that exposes SSH to the whole internet). Use your own IP as a /32."
  }
}

variable "region" {
  description = "GCP region to provision in."
  type        = string
  default     = "us-central1"
}

variable "name" {
  description = "Name applied to all resources (instance name, firewall name, network tag)."
  type        = string
  default     = "agent-keep"
}

# --- GCP-specific (no AWS analogue) ------------------------------------------

variable "project" {
  description = <<-EOT
    GCP project id to provision in. Defaults to agent-keep-kn6r6i — the dedicated
    project already created, billing-linked, and with the Compute Engine API
    enabled. Auth comes from Application Default Credentials
    (`gcloud auth application-default login`), NOT from anything in this module.
  EOT
  type        = string
  default     = "agent-keep-kn6r6i"
}

variable "zone" {
  description = "GCE zone within the region to place the instance. Default us-central1-a."
  type        = string
  default     = "us-central1-a"
}
