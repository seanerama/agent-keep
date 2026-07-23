# Agent Keep — AWS provisioner outputs: the UNIFORM interface GCP/Azure match.
# `ssh_target` is the single hand-off value bootstrap-host.sh + deploy-agent.sh
# consume (user@public-ip). Capture it with `terraform output -raw ssh_target`.

output "ssh_target" {
  description = "SSH target for the conformant host: ubuntu@<public-ip>. Feed to bootstrap-host.sh / deploy-agent.sh."
  value       = "ubuntu@${aws_instance.host.public_ip}"
}

output "instance_id" {
  description = "EC2 instance id."
  value       = aws_instance.host.id
}

output "public_ip" {
  description = "Public IPv4 address of the instance."
  value       = aws_instance.host.public_ip
}
