#!/bin/bash
# Builds dist/pve-ci-runners_<version>_all.deb and dist/SHA256SUMS from the checked-out tree. The same
# commit always gives the same bytes: every timestamp is the commit's and the archive order is sorted.
set -euo pipefail
cd "$(dirname "$0")/.."

version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}
export SOURCE_DATE_EPOCH
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/app" dist
cp -R src/pve_ci_runners "$work/app/"
find "$work/app" -name __pycache__ -prune -exec rm -rf {} +
find "$work/app" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

root="$work/root"
install -d "$root/DEBIAN" "$root/usr/bin" "$root/usr/lib/systemd/system" "$root/usr/share/doc/pve-ci-runners"
install -d -m 0700 "$root/etc/pve-ci-runners"
# zipapp's own -m entry point drops the return value, so every exit status would be 0.
printf 'import sys\n\nfrom pve_ci_runners.cli import main\n\nsys.exit(main())\n' >"$work/app/__main__.py"
touch -h -d "@$SOURCE_DATE_EPOCH" "$work/app/__main__.py"
# -I keeps the environment and the working directory out of the module search path of a root daemon.
python3 -m zipapp "$work/app" -p '/usr/bin/python3 -I' -c -o "$root/usr/bin/pve-ci-runners"
chmod 0755 "$root/usr/bin/pve-ci-runners"
install -m 0644 packaging/*.service packaging/*.timer "$root/usr/lib/systemd/system/"
install -m 0644 LICENSE "$root/usr/share/doc/pve-ci-runners/copyright"
install -m 0644 packaging/config.example.toml "$root/usr/share/doc/pve-ci-runners/"
cat >"$root/DEBIAN/control" <<CONTROL
Package: pve-ci-runners
Version: $version
Architecture: all
Maintainer: The pve-ci-runners contributors <https://github.com/DS09AT/pve-ci-runners/issues>
Depends: python3 (>= 3.13), qemu-server, pve-manager
Recommends: zfsutils-linux
Section: admin
Priority: optional
Homepage: https://github.com/DS09AT/pve-ci-runners
Description: GitHub Actions jobs in fresh Proxmox VE virtual machines
 Runs every GitHub Actions job of a GitHub App's private repositories in a
 fresh linked-clone VM with a per-job copy-on-write cache. Installing the
 package enables nothing; see /usr/share/doc/pve-ci-runners.
CONTROL
# dpkg runs both after the units were added or removed. Without a running systemd, as in a chroot or a
# container, there is nothing to reload; nothing is ever enabled or started.
cat >"$root/DEBIAN/postinst" <<'MAINTAINER_SCRIPT'
#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then
  systemctl --system daemon-reload >/dev/null || true
fi
MAINTAINER_SCRIPT
cp "$root/DEBIAN/postinst" "$root/DEBIAN/postrm"
chmod 0755 "$root/DEBIAN/postinst" "$root/DEBIAN/postrm"
find "$root" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

deb="pve-ci-runners_${version}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$root" "dist/$deb" >/dev/null
(cd dist && sha256sum "$deb" >SHA256SUMS)
echo "dist/$deb"
