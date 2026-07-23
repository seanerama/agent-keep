# Agent Keep — AWS provisioner: a CONFORMANT host (ADR 0007/0009). The module's
# whole job is "make an Ubuntu 24.04 x86_64 VM exist, SSH-reachable, firewalled"
# — it does NOT install the chassis or the docker topology (that is
# bootstrap-host.sh + deploy.sh's job on ANY Ubuntu+Docker VM). Its output is an
# ssh target the existing bootstrap + deploy-agent.sh consume unchanged.

provider "aws" {
  region = var.region
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

# Ubuntu 24.04 LTS (Noble), x86_64 — looked up live from Canonical's owner id
# (099720109477), NEVER a hardcoded stale AMI id. most_recent picks the newest
# matching image at apply time.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

# Register the operator's EXISTING public key so `ssh ubuntu@<ip>` works.
resource "aws_key_pair" "operator" {
  key_name   = "${var.name}-key"
  public_key = local.ssh_public_key_material

  tags = {
    Name = var.name
  }
}

# Security group: SSH (22) from the operator's CIDR ONLY; egress fully open for
# image pulls + model-provider APIs. Dev-http (8377/8477) is NOT opened — it
# stays on host loopback, reached via an SSH tunnel (ADR 0007 flexible access).
resource "aws_security_group" "host" {
  name        = "${var.name}-sg"
  description = "Agent Keep conformant host: SSH from operator CIDR only, egress open."

  ingress {
    description = "SSH from the operator's allowed CIDR only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  egress {
    description = "All egress (container image pulls + model-provider APIs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = var.name
  }
}

resource "aws_instance" "host" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_size
  key_name               = aws_key_pair.operator.key_name
  vpc_security_group_ids = [aws_security_group.host.id]

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  tags = {
    Name = var.name
  }
}
