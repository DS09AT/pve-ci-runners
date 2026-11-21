#!/bin/bash
# Proves from inside the VM that the hypervisor is out of reach before a job may run here.
# Exit 0: isolated. Exit 3: something answered.
# Inputs: GATEWAY, HOST_ADDRESSES (every global address of the hypervisor, space-separated).
set -euo pipefail

# The attempts run in parallel, so a host with many addresses still fits the controller's timeout.
pids=()
# shellcheck disable=SC2086 # HOST_ADDRESSES is a space-separated list of validated addresses
for address in "$GATEWAY" $HOST_ADDRESSES; do
  for port in 22 8006 3128; do
    (
      if timeout 3 bash -c "exec 3<>/dev/tcp/$address/$port" 2>/dev/null; then
        echo "the hypervisor answered on $address port $port" >&2
        exit 3
      fi
    ) &
    pids+=("$!")
  done
done
answered=0
for pid in "${pids[@]}"; do
  wait "$pid" || answered=1
done
[ "$answered" -eq 0 ] || exit 3
if ip -6 -o addr show scope link 2>/dev/null | grep -q inet6; then
  echo "IPv6 is enabled" >&2
  exit 3
fi
