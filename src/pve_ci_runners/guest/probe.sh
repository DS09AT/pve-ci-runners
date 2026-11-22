#!/bin/bash
# Proves from inside the VM that the hypervisor and the denied destinations are out of reach before a
# job may run here.
# Exit 0: isolated. Exit 3: something answered.
# Inputs: GATEWAY, HOST_ADDRESSES (every global address of the hypervisor, space-separated),
# DENY_SAMPLE (one address in each of some denied networks, space-separated, may be empty).
set -euo pipefail

# The attempts run in parallel, so a host with many addresses still fits the controller's timeout.
pids=()
# shellcheck disable=SC2086 # both lists are space-separated, validated addresses
for address in "$GATEWAY" $HOST_ADDRESSES $DENY_SAMPLE; do
  for port in 22 8006 3128; do
    (
      if timeout 3 bash -c "exec 3<>/dev/tcp/$address/$port" 2>/dev/null; then
        echo "a denied destination answered on $address port $port" >&2
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
