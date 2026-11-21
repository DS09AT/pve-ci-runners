#!/bin/bash
# Node.js in the tool cache layout setup-node looks in first. Inputs: VERSION.
set -euo pipefail

file="node-v$VERSION-linux-x64.tar.xz"
curl -fsSL -o "/tmp/$file" "https://nodejs.org/dist/v$VERSION/$file"
curl -fsSL -o /tmp/SHASUMS256.txt "https://nodejs.org/dist/v$VERSION/SHASUMS256.txt"
(cd /tmp && grep "  $file\$" SHASUMS256.txt | sha256sum -c --quiet -)
target="/opt/hostedtoolcache/node/$VERSION/x64"
install -d "$target"
tar -xJf "/tmp/$file" -C "$target" --strip-components=1
rm "/tmp/$file" /tmp/SHASUMS256.txt
touch "$target.complete"
ln -sf "$target/bin/node" /usr/local/bin/node
ln -sf "$target/bin/npm" /usr/local/bin/npm
ln -sf "$target/bin/npx" /usr/local/bin/npx
