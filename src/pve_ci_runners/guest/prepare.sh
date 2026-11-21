#!/bin/bash
# Gives a fresh clone its address and installs the runner unit and job hook. Runs once per VM, as root,
# through the guest agent. Inputs: ADDRESS PREFIX GATEWAY DNS HOSTNAME.
set -euo pipefail

ifc=$(find /sys/class/net -mindepth 1 -maxdepth 1 ! -name lo -printf '%f\n' | sort | head -n 1)
sysctl -qw "net.ipv6.conf.all.disable_ipv6=1" "net.ipv6.conf.default.disable_ipv6=1"
ip link set "$ifc" up
ip addr replace "$ADDRESS/$PREFIX" dev "$ifc"
ip route replace default via "$GATEWAY"
# shellcheck disable=SC2086 # DNS is a space-separated list of validated addresses
resolvectl dns "$ifc" $DNS
resolvectl domain "$ifc" '~.'
hostnamectl set-hostname "$HOSTNAME"

# Hosted runner images raise these limits; test suites with many file watchers fail without them.
sysctl -qw fs.inotify.max_user_instances=1280 fs.inotify.max_user_watches=655360
[ -e /usr/local/bin/python ] || ln -s /usr/bin/python3 /usr/local/bin/python

install -d -m 0755 /opt/pve-ci-runners
install -d -m 0700 -o runner -g runner /run/ci-runner
# The job runs as the runner user, so the marker of the runner's exit lives where that user cannot write.
install -d -m 0755 -o root -g root /run/ci-runner-state
cat >/opt/pve-ci-runners/job-started.sh <<'HOOK'
#!/bin/sh
# Tells the controller which run this runner took, then waits until the controller has attached the
# job's cache. The report is only a hint: the controller verifies it against the GitHub API.
printf '{"run_id":%s}\n' "${GITHUB_RUN_ID:-0}" >/run/ci-runner/job.json
i=0
while [ ! -f /run/ci-runner/cache-ready ] && [ "$i" -lt 240 ]; do
  sleep 0.5
  i=$((i + 1))
done
[ -f /run/ci-runner/cache-ready ] || sudo systemctl start docker
HOOK
cat >/opt/pve-ci-runners/start-runner.sh <<'START'
#!/bin/sh
# The single-use runner configuration is read once and removed before the job can see it.
config=$(cat /run/ci-runner/jit)
rm -f /run/ci-runner/jit
exec /opt/actions-runner/run.sh --jitconfig "$config"
START
chmod 0755 /opt/pve-ci-runners/job-started.sh /opt/pve-ci-runners/start-runner.sh
cat >/etc/systemd/system/ci-runner.service <<'UNIT'
[Unit]
Description=GitHub Actions runner for exactly one job
After=network-online.target

[Service]
User=runner
WorkingDirectory=/opt/actions-runner
Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/pve-ci-runners/job-started.sh
Environment=LANG=C.UTF-8
LimitNOFILE=65536
ExecStart=/opt/pve-ci-runners/start-runner.sh
ExecStopPost=+/usr/bin/touch /run/ci-runner-state/done
Restart=no
UNIT
systemctl daemon-reload
