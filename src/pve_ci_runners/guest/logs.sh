#!/bin/bash
# The journal and kernel log of this boot, which vanish with the VM.
set -uo pipefail

journalctl -b --no-pager -o short-iso | tail -n 400
echo '== dmesg'
dmesg | tail -n 80
