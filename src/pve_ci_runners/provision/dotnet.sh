#!/bin/bash
# The .NET SDK of one channel where setup-dotnet looks first. Inputs: VERSION (a channel).
set -euo pipefail

curl -fsSL -o /tmp/dotnet-install.sh https://dot.net/v1/dotnet-install.sh
bash /tmp/dotnet-install.sh --channel "$VERSION" --install-dir /opt/hostedtoolcache/dotnet >/dev/null
rm /tmp/dotnet-install.sh
