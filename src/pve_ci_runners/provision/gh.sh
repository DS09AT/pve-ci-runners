#!/bin/bash
# The GitHub CLI, which hosted runner images ship and release workflows call.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Only the pinned certificate is installed: apt trusts every key in a signed-by keyring, so a second key
# appended to the download would otherwise sign the repository too.
fingerprint=2C6106201985B60E6C7AC87323F3D4EA75716059
install -d -m 0755 /etc/apt/keyrings
download=$(mktemp)
pinned=$(mktemp)
GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME" "$download" "$pinned"' EXIT
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$download"
gpg --batch --quiet --import "$download"
gpg --batch --export --export-options export-minimal "$fingerprint" >"$pinned"
# gpg --export succeeds with no output for an unknown key, so the result itself is checked.
primaries=$(gpg --show-keys --with-colons "$pinned" | awk -F: '$1 == "pub" {p = 1; next} p && $1 == "fpr" {print $10; p = 0}')
[ "$primaries" = "$fingerprint" ] || {
  echo "unexpected GitHub CLI signing key" >&2
  exit 1
}
install -m 0644 "$pinned" /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  >/etc/apt/sources.list.d/github-cli.list
apt-get update -q >/dev/null
apt-get install -y -q gh >/dev/null
