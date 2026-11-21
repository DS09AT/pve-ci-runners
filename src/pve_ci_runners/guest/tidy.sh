#!/bin/bash
# Ages out unused packages and images and bounds the volume before it becomes the repository's cache.
# Inputs: UNUSED_DAYS DOCKER_KEEP_GB WIPE_ABOVE_MB MOUNT_PATHS (one path per line).
set -uo pipefail

find /cache/nuget/packages -mindepth 2 -maxdepth 2 -type d -atime "+$UNUSED_DAYS" -exec rm -rf {} + 2>/dev/null
[ -d /home/runner/.npm ] && sudo -u runner npm cache verify --cache /home/runner/.npm >/dev/null 2>&1
docker builder prune -f --keep-storage "${DOCKER_KEEP_GB}GB" >/dev/null 2>&1
docker image prune -a -f --filter "until=$((UNUSED_DAYS * 24))h" >/dev/null 2>&1
if [ "$(du -sm /cache | cut -f1)" -gt "$WIPE_ABOVE_MB" ]; then
  rm -rf /cache/nuget/* /cache/npm/*
fi
systemctl stop ci-runner docker.socket docker.service containerd.service 2>/dev/null
while read -r path; do
  [ -n "$path" ] && umount "$path"
done <<<"$MOUNT_PATHS"
fstrim /cache
umount /cache
sync
