#!/bin/bash
# Python in the tool cache layout setup-python looks in first. Inputs: VERSION.
set -euo pipefail

# shellcheck source=/dev/null # the image's release file, read for VERSION_ID
. /etc/os-release
url=$(curl -fsSL https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json |
  jq -r --arg v "$VERSION" --arg os "$VERSION_ID" \
    '.[] | select(.version == $v) | .files[]
     | select(.platform == "linux" and .platform_version == $os and .arch == "x64") | .download_url' |
  head -n 1)
[[ $url =~ ^https://github\.com/actions/python-versions/releases/download/([^/]+)/([^/]+)$ ]] || {
  echo "no Python $VERSION build for this image" >&2
  exit 1
}
tag=${BASH_REMATCH[1]}
asset=${BASH_REMATCH[2]}
digest=$(curl -fsSL "https://api.github.com/repos/actions/python-versions/releases/tags/$tag" |
  jq -r --arg name "$asset" '.assets[] | select(.name == $name) | .digest')
[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "the Python release $tag publishes no digest for $asset" >&2
  exit 1
}
curl -fsSL -o /tmp/python.tgz "$url"
echo "${digest#sha256:}  /tmp/python.tgz" | sha256sum -c --quiet -
rm -rf /tmp/python
mkdir /tmp/python
tar -xzf /tmp/python.tgz -C /tmp/python
rm /tmp/python.tgz
(cd /tmp/python && RUNNER_TOOL_CACHE=/opt/hostedtoolcache AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache bash ./setup.sh >/dev/null)
rm -rf /tmp/python
