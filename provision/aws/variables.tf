# Agent Keep — AWS provisioner: the UNIFORM provisioner interface (ADR 0009).
# GCP and Azure modules land later exposing these SAME variable names, so the
# operator workflow (and any wrapper) is identical across clouds. Only the
# defaults that are genuinely cloud-shaped (region, instance size) differ.

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
  description = "EC2 instance type. MUST be x86_64/amd64 (ADR 0009 — chassis images are amd64, not Graviton/arm64). Default t3.small."
  type        = string
  default     = "t3.small"
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
  description = "AWS region to provision in."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Name applied to all resources (tags, key-pair name, SG name)."
  type        = string
  default     = "agent-keep"
}
