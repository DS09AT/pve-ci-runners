#!/bin/bash
# Prints the hook's report once a job has started and the runner is still running it.
set -euo pipefail

[ -f /run/ci-runner-state/done ] || cat /run/ci-runner/job.json 2>/dev/null || true
