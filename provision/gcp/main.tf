# Agent Keep — GCP provisioner: a CONFORMANT host (ADR 0007/0009). The module's
# whole job is "make an Ubuntu 24.04 x86_64 VM exist, SSH-reachable, firewalled"
# — it does NOT install the chassis or the docker topology (that is
# bootstrap-host.sh + deploy.sh's job on ANY Ubuntu+Docker VM). Its output is an
# ssh target the existing bootstrap + deploy-agent.sh consume unchanged.
# Mirrors provision/aws/main.tf on the same uniform interface, google provider.

# Auth comes from Application Default Credentials (the operator's
# `gcloud auth application-default login`) — NEVER creds in the module.
provider "google" {
  project = var.project
  region  = var.region
}

# The ssh key may be inline material ("ssh-ed25519 AAAA...") or a path to a .pub
# file — resolve both to material here so the operator can pass either.
locals {
  ssh_public_key_material = (
    startswith(trimspace(var.ssh_public_key), "ssh-")
    ? var.ssh_public_key
    : file(pathexpand(var.ssh_public_key))
  )
}

# Ubuntu 24.04 LTS (Noble), x86_64 — looked up live from Canonical's public
# image family in ubuntu-os-cloud, NEVER a hardcoded stale image. The family
# resolves the newest matching image at apply time.
data "google_compute_image" "ubuntu" {
  family  = "ubuntu-2404-lts-amd64"
  project = "ubuntu-os-cloud"
}

# Firewall: SSH (22) from the operator's CIDR ONLY, scoped by the instance's
# network tag; egress is open by default on GCP (image pulls + model-provider
# APIs). Dev-http (8377/8477) is NOT opened — it stays on host loopback, reached
# via an SSH tunnel (ADR 0007 flexible access).
resource "google_compute_firewall" "ssh" {
  name        = "${var.name}-ssh"
  network     = "default"
  description = "Agent Keep conformant host: SSH from operator CIDR only."

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = [var.allowed_ssh_cidr]
  target_tags   = [var.name]
}

resource "google_compute_instance" "host" {
  name         = var.name
  machine_type = var.instance_size
  zone         = var.zone
  tags         = [var.name]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = "default"

    # An access_config with no fields provisions an ephemeral external IP so the
    # host is SSH-reachable from the operator's CIDR.
    access_config {}
  }

  # Register the operator's EXISTING public key for the `ubuntu` login so
  # `ssh ubuntu@<ip>` works.
  metadata = {
    ssh-keys = "ubuntu:${local.ssh_public_key_material}"
  }
}
