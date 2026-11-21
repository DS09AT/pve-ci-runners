#!/bin/bash
# Seals the template: hands the tool cache to the job user, removes everything per-machine and prints
# the runner version, which the build records on the template.
set -euo pipefail

chown -R runner:runner /opt/hostedtoolcache
rm -rf /opt/actions-runner/_diag /root/.gnupg
# The build's own address must not come back in a clone; the controller configures each one.
rm -f /etc/netplan/*.yaml
touch /etc/cloud/cloud-init.disabled
apt-get clean
cloud-init clean --logs >/dev/null 2>&1 || true
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
fstrim -a >/dev/null 2>&1 || true
sudo -u runner /opt/actions-runner/bin/Runner.Listener --version 2>/dev/null | tail -n 1
