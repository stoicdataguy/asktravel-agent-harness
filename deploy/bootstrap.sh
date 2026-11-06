#!/usr/bin/env bash
# Turns a fresh Ubuntu 24.04 arm64 server into an AskTravel host (#30). EC2 runs it once as the
# instance's user data, as root. It's idempotent, so running it again by hand is safe.
# Log: /var/log/cloud-init-output.log. Finished when /var/lib/asktravel-bootstrapped exists.
set -euo pipefail

REGION=us-east-2
APP_DIR=/opt/asktravel
REPO_URL=https://github.com/stoicdataguy/asktravel-agent-harness.git
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get install -yq ca-certificates curl git unzip unattended-upgrades
systemctl enable --now unattended-upgrades

# Docker, from Docker's own repository
if ! docker compose version >/dev/null 2>&1; then
  apt-get remove -yq docker.io docker-compose-v2 containerd runc || true
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker
docker info >/dev/null  # stop here if the daemon isn't really up
# After Docker, and without recommends: it would otherwise pull in Ubuntu's older docker.io
apt-get install -yq --no-install-recommends amazon-ecr-credential-helper

# AWS CLI v2: deploy.sh reads secrets and guidebooks with it, using the instance role
if ! command -v aws >/dev/null; then
  curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip -o /tmp/awscliv2.zip
  unzip -q /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi

# `docker pull` from ECR logs in with the instance role, so no credentials are stored
ACCOUNT=$(aws sts get-caller-identity --region "$REGION" --query Account --output text)
mkdir -p /root/.docker
cat >/root/.docker/config.json <<EOF
{"credHelpers": {"$ACCOUNT.dkr.ecr.$REGION.amazonaws.com": "ecr-login"}}
EOF

# 2 GB of swap as a safety net: a memory spike gets slow instead of killing a process
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi
swapon --show | grep -q /swapfile || swapon /swapfile

# Preinstalled on Ubuntu's AMIs; Session Manager and any later SSM commands depend on it
systemctl is-active --quiet snap.amazon-ssm-agent.amazon-ssm-agent.service

[ -d "$APP_DIR/.git" ] || git clone --quiet "$REPO_URL" "$APP_DIR"

touch /var/lib/asktravel-bootstrapped
echo "bootstrap finished"
