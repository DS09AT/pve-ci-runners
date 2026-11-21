#!/bin/bash
# Go in the tool cache layout setup-go looks in first, and on PATH. Inputs: VERSION.
set -euo pipefail

file="go$VERSION.linux-amd64.tar.gz"
sha=$(curl -fsSL 'https://go.dev/dl/?mode=json&include=all' |
  jq -r --arg file "$file" '.[].files[] | select(.filename == $file) | .sha256')
[[ $sha =~ ^[0-9a-f]{64}$ ]] || {
  echo "go.dev lists no checksum for $file" >&2
  exit 1
}
curl -fsSL -o "/tmp/$file" "https://go.dev/dl/$file"
echo "$sha  /tmp/$file" | sha256sum -c --quiet -
target="/opt/hostedtoolcache/go/$VERSION/x64"
install -d "$target"
tar -xzf "/tmp/$file" -C "$target" --strip-components=1
rm "/tmp/$file"
touch "$target.complete"
ln -sf "$target/bin/go" /usr/local/bin/go
ln -sf "$target/bin/gofmt" /usr/local/bin/gofmt
