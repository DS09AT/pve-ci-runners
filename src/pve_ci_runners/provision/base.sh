#!/bin/bash
# The base of every runner template: the job user, the newest Actions runner and a locked-down system.
# Runs once as root in a fresh cloud-image VM with outbound access, through the guest agent.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -q >/dev/null
apt-get purge -y -q snapd >/dev/null 2>&1 || true
apt-get install -y -q --no-install-recommends git jq curl unzip zip build-essential python3 \
  python-is-python3 ca-certificates sudo file nftables >/dev/null
apt-get autoremove -y -q >/dev/null

useradd --create-home --shell /bin/bash runner
# Hosted runners give jobs passwordless sudo; workflows rely on it. The VM is discarded after one job.
echo 'runner ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/runner
chmod 0440 /etc/sudoers.d/runner
install -d -o runner -g runner /home/runner/.nuget /home/runner/.npm /opt/hostedtoolcache /opt/actions-runner

# GitHub publishes a SHA-256 digest for every release asset; the download must match it.
release=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest)
version=$(jq -r '.tag_name | ltrimstr("v")' <<<"$release")
asset="actions-runner-linux-x64-$version.tar.gz"
url=$(jq -r --arg name "$asset" '.assets[] | select(.name == $name) | .browser_download_url' <<<"$release")
digest=$(jq -r --arg name "$asset" '.assets[] | select(.name == $name) | .digest' <<<"$release")
[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "the runner release $version publishes no digest for $asset" >&2
  exit 1
}
curl -fsSL -o /tmp/runner.tgz "$url"
echo "${digest#sha256:}  /tmp/runner.tgz" | sha256sum -c --quiet -
tar -xzf /tmp/runner.tgz -C /opt/actions-runner
rm /tmp/runner.tgz
/opt/actions-runner/bin/installdependencies.sh >/dev/null
chown -R runner:runner /opt/actions-runner

fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap -q /swapfile
echo '/swapfile none swap sw 0 0' >>/etc/fstab

cat >/etc/sysctl.d/90-ci-runner.conf <<'SYSCTL'
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
kernel.unprivileged_bpf_disabled = 1
net.core.bpf_jit_harden = 2
fs.suid_dumpable = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
vm.swappiness = 10
fs.inotify.max_user_instances = 1280
fs.inotify.max_user_watches = 655360
SYSCTL

# Nothing listens for the outside; the hypervisor reaches the VM only through the guest agent.
cat >/etc/nftables.conf <<'NFT'
#!/usr/sbin/nft -f
table inet ci_runner_guard
delete table inet ci_runner_guard
table inet ci_runner_guard {
  chain input {
    type filter hook input priority filter; policy drop;
    iif "lo" accept
    ct state established,related accept
  }
}
NFT
systemctl enable -q nftables

# The controller addresses each clone itself; nothing may wait for or configure the link.
systemctl mask -q systemd-networkd-wait-online.service
# Runner VMs are patched by rebuilding the template, not by jobs racing apt.
systemctl disable -q apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service 2>/dev/null || true
systemctl mask -q fwupd.service fwupd-refresh.timer 2>/dev/null || true
