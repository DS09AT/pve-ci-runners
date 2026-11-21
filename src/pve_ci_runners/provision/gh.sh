#!/bin/bash
# The GitHub CLI, which hosted runner images ship and release workflows call.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Only the pinned certificates are installed: apt trusts every key in a signed-by keyring, so a key
# appended to the download would otherwise sign the repository too. Both are the fingerprints GitHub
# publishes in the gh install docs; the repository is signed with the newer one.
fingerprints="2C6106201985B60E6C7AC87323F3D4EA75716059 7F38BBB59D064DBCB3D84D725612B36462313325"
install -d -m 0755 /etc/apt/keyrings
download=$(mktemp)
pinned=$(mktemp)
GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME" "$download" "$pinned"' EXIT
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$download"
gpg --batch --quiet --import "$download"
# shellcheck disable=SC2086 # a space-separated list of fingerprints
gpg --batch --export --export-options export-minimal $fingerprints >"$pinned"
# gpg --export succeeds with no output for an unknown key, so the result itself is checked.
primaries=$(gpg --show-keys --with-colons "$pinned" | awk -F: '$1 == "pub" {p = 1; next} p && $1 == "fpr" {print $10; p = 0}')
[ -n "$primaries" ] || {
  echo "the GitHub CLI keyring holds none of the pinned keys" >&2
  exit 1
}
for primary in $primaries; do
  case " $fingerprints " in
    *" $primary "*) ;;
    *)
      echo "unexpected GitHub CLI signing key $primary" >&2
      exit 1
      ;;
  esac
done
install -m 0644 "$pinned" /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  >/etc/apt/sources.list.d/github-cli.list
apt-get update -q >/dev/null
apt-get install -y -q gh >/dev/null
