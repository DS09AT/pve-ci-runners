#!/bin/bash
# Docker for jobs. It starts only once the controller has mounted the job's cache, which carries its
# image store, so the store of one job never leaks into the template.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get install -y -q --no-install-recommends docker.io docker-buildx docker-compose-v2 >/dev/null
usermod -aG docker runner
install -d -m 0755 /etc/docker
cat >/etc/docker/daemon.json <<'JSON'
{
  "log-driver": "local",
  "log-opts": {"max-size": "20m", "max-file": "3"},
  "default-address-pools": [{"base": "172.18.0.0/15", "size": 24}],
  "userland-proxy": false
}
JSON
systemctl disable -q --now docker.service docker.socket containerd.service
