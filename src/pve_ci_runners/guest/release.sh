#!/bin/bash
# Lets a job whose cache could not be attached run without one instead of waiting for it.
set -euo pipefail

systemctl start docker || true
touch /run/ci-runner/cache-ready
