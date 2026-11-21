#!/bin/bash
# Stores the single-use runner configuration from stdin and starts the runner.
set -euo pipefail

install -m 0600 -o runner -g runner /dev/stdin /run/ci-runner/jit
rm -rf /opt/actions-runner/_diag
systemctl start ci-runner
