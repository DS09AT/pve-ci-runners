#!/bin/bash
# Prints the hook's report, or an empty object, once the runner has exited.
set -euo pipefail

if [ -f /run/ci-runner-state/done ]; then
  cat /run/ci-runner/job.json 2>/dev/null || echo '{}'
fi
