# Agent Keep — GCP provisioner outputs: the UNIFORM interface AWS/Azure match.
# `ssh_target` is the single hand-off value bootstrap-host.sh + deploy-agent.sh
# consume (user@public-ip). Capture it with `terraform output -raw ssh_target`.

output "ssh_target" {
  description = "SSH target for the conformant host: ubuntu@<public-ip>. Feed to bootstrap-host.sh / deploy-agent.sh."
  value       = "ubuntu@${google_compute_instance.host.network_interface[0].access_config[0].nat_ip}"
}

output "instance_id" {
  description = "GCE instance id."
  value       = google_compute_instance.host.instance_id
}

output "public_ip" {
  description = "Public (ephemeral external) IPv4 address of the instance."
  value       = google_compute_instance.host.network_interface[0].access_config[0].nat_ip
}
