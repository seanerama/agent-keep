# Agent Keep — AWS provisioner: Terraform + provider version constraints.
# ADR 0009: local gitignored state, no remote backend (deferred). The AWS
# provider is pinned to a major (5.x) so `terraform validate` in CI resolves a
# stable schema; required_version floors at a Terraform that supports the
# language features used here (startswith/pathexpand, nested validation).
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
