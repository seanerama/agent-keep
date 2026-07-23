# Agent Keep — GCP provisioner: Terraform + provider version constraints.
# ADR 0009: local gitignored state, no remote backend (deferred). The google
# provider is pinned to a major (5.x) so `terraform validate` in CI resolves a
# stable schema; required_version floors at a Terraform that supports the
# language features used here (startswith/pathexpand, nested validation).
# Mirrors provision/aws/versions.tf on the same uniform interface.
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}
