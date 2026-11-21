#!/bin/bash
# Mounts the job's cache disk, found by its serial, and binds its directories into place.
# Inputs: SERIAL, MOUNTS (one "directory path owner" triple per line).
set -euo pipefail

disk=""
for _ in $(seq 50); do
  disk=$(find /dev/disk/by-id -name "*_${SERIAL}" ! -name '*-part*' -print -quit)
  [ -n "$disk" ] && break
  sleep 0.2
done
[ -n "$disk" ] || {
  echo "no disk with serial $SERIAL" >&2
  exit 1
}
mkdir -p /cache
mount -o discard "$disk" /cache
while read -r directory path owner; do
  [ -n "$directory" ] || continue
  install -d -o "$owner" -g "$owner" "/cache/$directory" "$path"
  mount --bind "/cache/$directory" "$path"
done <<<"$MOUNTS"
systemctl start docker
touch /run/ci-runner/cache-ready
