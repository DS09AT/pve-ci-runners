"""Checks the node and the GitHub App against what the controller needs; each failure names its fix.

The checks only read. A per-VM firewall without the datacenter firewall is inert, a backup job that locks
a runner VM stops its removal, and a missing App permission fails jobs only at runtime, so each of these
is cheaper to find here than in a failed job.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pve_ci_runners.config import Config, check_secret_file
from pve_ci_runners.errors import PveCiError
from pve_ci_runners.ports import GitHubApp, Hypervisor
from pve_ci_runners.proc import Commands, output
from pve_ci_runners.pve import TAG, datacenter_firewall_enabled
from pve_ci_runners.templates import current_template

if TYPE_CHECKING:
    from collections.abc import Callable

Level = Literal["ok", "warn", "fail"]
_LEVELS = {"read": 1, "write": 2, "admin": 3}
_LINKED_CLONE_TYPES = frozenset({"zfspool", "lvmthin", "rbd"})


@dataclass(frozen=True)
class Finding:
    """The outcome of one check."""

    level: Level
    check: str
    detail: str
    fix: str = ""

    def to_text(self) -> str:
        """One line, and the fix below it when there is one."""
        line = f"{self.level.upper():<5} {self.check}: {self.detail}"
        return f"{line}\n      fix: {self.fix}" if self.fix else line


class Doctor:
    """Runs every check; a check that cannot run is a failure, never a pass."""

    def __init__(self, config: Config, commands: Commands, hypervisor: Hypervisor, github: GitHubApp) -> None:
        self._config = config
        self._commands = commands
        self._pve = hypervisor
        self._github = github

    def run(self) -> list[Finding]:
        """Every finding, in the order an operator would fix them."""
        checks: list[tuple[str, Callable[[], Finding]]] = [
            ("app key", self._key),
            ("datacenter firewall", self._firewall),
            ("bridge", self._bridge),
            ("forwarding", self._forwarding),
            ("outbound NAT", self._nat),
            ("template storage", self._template_storage),
            ("snippet storage", self._snippet_storage),
            ("cache dataset", self._dataset),
            ("VMIDs", self._vmids),
            ("backup jobs", self._backups),
            ("GitHub App", self._app),
            ("template", self._template),
        ]
        findings: list[Finding] = []
        for name, check in checks:
            try:
                findings.append(check())
            except PveCiError as err:
                findings.append(Finding("fail", name, f"could not be checked: {err}"))
        return findings

    def _key(self) -> Finding:
        check_secret_file(self._config.github.app_key_file)
        return Finding("ok", "app key", f"{self._config.github.app_key_file} is private to its owner")

    def _firewall(self) -> Finding:
        if datacenter_firewall_enabled(self._commands):
            return Finding("ok", "datacenter firewall", "enabled")
        return Finding(
            "fail",
            "datacenter firewall",
            "disabled, so no VM firewall rule takes effect",
            "allow your own SSH and port 8006 in the datacenter rules, then "
            "pvesh set /cluster/firewall/options --enable 1",
        )

    def _bridge(self) -> Finding:
        network = self._config.network
        links = json.loads(
            output(self._commands, ["ip", "-j", "addr", "show", "dev", network.bridge], timeout=30)
        )
        found = {
            (str(a.get("local")), a.get("prefixlen"))
            for link in _dicts(links)
            for a in _dicts(link.get("addr_info"))
        }
        if (str(network.gateway), network.prefix) in found:
            return Finding("ok", "bridge", f"{network.bridge} carries {network.gateway}/{network.prefix}")
        return Finding(
            "fail",
            "bridge",
            f"{network.bridge} does not carry {network.gateway}/{network.prefix}",
            f"add the address to {network.bridge} in /etc/network/interfaces, then ifreload -a",
        )

    def _forwarding(self) -> Finding:
        value = output(self._commands, ["sysctl", "-n", "net.ipv4.ip_forward"], timeout=30).strip()
        if value == "1":
            return Finding("ok", "forwarding", "IPv4 forwarding is on")
        return Finding(
            "fail",
            "forwarding",
            "IPv4 forwarding is off, so runner VMs have no way out",
            "echo net.ipv4.ip_forward=1 > /etc/sysctl.d/90-pve-ci-runners.conf && sysctl --system",
        )

    def _nat(self) -> Finding:
        network = str(self._config.network.network)
        ruleset = output(self._commands, ["nft", "list", "ruleset"], timeout=30)
        if any(network in line and ("masquerade" in line or "snat" in line) for line in ruleset.splitlines()):
            return Finding("ok", "outbound NAT", f"{network} is masqueraded")
        return Finding(
            "warn",
            "outbound NAT",
            f"no masquerade or snat rule for {network} found in nftables",
            f"add a post-up rule for {network} to the bridge in /etc/network/interfaces",
        )

    def _template_storage(self) -> Finding:
        storage = self._config.template.storage
        info = self._storage(storage)
        content = str(info.get("content", ""))
        if "images" not in content.split(","):
            return Finding(
                "fail",
                "template storage",
                f"{storage} does not hold VM disks",
                f"pvesm set {storage} --content {_with_content(content, 'images')}",
            )
        if info.get("type") not in _LINKED_CLONE_TYPES:
            return Finding(
                "warn",
                "template storage",
                f"{storage} is of type {info.get('type')}; linked clones are fastest on zfspool or lvmthin",
            )
        return Finding("ok", "template storage", f"{storage} ({info.get('type')}) takes linked clones")

    def _snippet_storage(self) -> Finding:
        storage = self._config.template.snippet_storage
        content = str(self._storage(storage).get("content", ""))
        if "snippets" in content.split(","):
            return Finding("ok", "snippet storage", f"{storage} holds snippets")
        return Finding(
            "fail",
            "snippet storage",
            f"{storage} does not hold snippets, which a template build needs for cloud-init",
            f"pvesm set {storage} --content {_with_content(content, 'snippets')}",
        )

    def _dataset(self) -> Finding:
        cache = self._config.cache
        if cache.backend != "zfs":
            return Finding("ok", "cache dataset", "no cache configured")
        found = self._commands.run(["zfs", "list", "-H", "-o", "name", cache.dataset], timeout=30)
        if found.returncode == 0:
            return Finding("ok", "cache dataset", f"{cache.dataset} exists")
        return Finding(
            "fail", "cache dataset", f"{cache.dataset} does not exist", f"zfs create -p {cache.dataset}"
        )

    def _vmids(self) -> Finding:
        prefix = self._config.daemon.name_prefix
        expected = {s.vmid: f"{prefix}{s.vmid}" for s in self._config.slots}
        expected |= {v: f"{prefix}template-{v}" for v in self._config.template.vmids}
        vms = self._pve.vms()
        foreign = sorted(
            vmid
            for vmid, name in expected.items()
            if vmid in vms and (TAG not in vms[vmid].tags or vms[vmid].name != name)
        )
        if not foreign:
            return Finding("ok", "VMIDs", "every slot and template VMID is free or ours")
        listed = ", ".join(str(v) for v in foreign)
        return Finding(
            "fail",
            "VMIDs",
            f"{listed} hold VMs that pve-ci-runners did not create; they are left alone and stay unusable",
            "move those VMs or configure other VMIDs",
        )

    def _backups(self) -> Finding:
        ours = {s.vmid for s in self._config.slots} | set(self._config.template.vmids)
        jobs = _dicts(
            json.loads(output(self._commands, ["pvesh", "get", "/cluster/backup", "--output-format", "json"]))
        )
        covering = {str(job.get("id", "?")): fix for job in jobs if (fix := _backup_fix(job, ours))}
        if not covering:
            return Finding("ok", "backup jobs", "no backup job includes a runner VM")
        return Finding(
            "fail",
            "backup jobs",
            f"{', '.join(covering)} include runner VMs; a backup locks a VM the controller must remove",
            "; ".join(covering.values()),
        )

    def _app(self) -> Finding:
        needed = {"administration": "write", "actions": "read", "metadata": "read", "contents": "read"}
        if self._config.overflow is not None:
            needed["variables"] = "write"
        granted = self._github.permissions()
        if "actions_variables" in granted:
            granted.setdefault("variables", granted["actions_variables"])
        missing = sorted(
            f"{name}: {level}"
            for name, level in needed.items()
            if _LEVELS.get(granted.get(name, ""), 0) < _LEVELS[level]
        )
        if not missing:
            return Finding("ok", "GitHub App", "installed with every permission the controller needs")
        return Finding(
            "fail",
            "GitHub App",
            f"missing {', '.join(missing)}",
            "grant them in the App settings, then accept them in the installation",
        )

    def _template(self) -> Finding:
        found = current_template(
            self._config.template.vmids,
            self._pve.vms(),
            lambda v: str(self._pve.config(v).get("description", "")),
        )
        if found is None:
            return Finding("fail", "template", "no finished template", "pve-ci-runners template build")
        vmid, marker = found
        release = self._github.get("/repos/actions/runner/releases/latest")
        latest = (
            str(cast("dict[str, object]", release).get("tag_name", "")).removeprefix("v")
            if isinstance(release, dict)
            else ""
        )
        if latest and latest != marker.runner_version:
            return Finding(
                "warn",
                "template",
                f"{vmid} carries runner {marker.runner_version}, the newest release is {latest}",
                "pve-ci-runners template build",
            )
        return Finding("ok", "template", f"{vmid} carries runner {marker.runner_version}")

    def _storage(self, storage: str) -> dict[str, object]:
        return self._json(["pvesh", "get", f"/storage/{storage}", "--output-format", "json"])

    def _json(self, argv: list[str]) -> dict[str, object]:
        try:
            value = json.loads(output(self._commands, argv, timeout=30))
        except ValueError:
            raise PveCiError(f"{' '.join(argv[:3])} returned no JSON") from None
        if not isinstance(value, dict):
            raise PveCiError(f"{' '.join(argv[:3])} returned no object")
        return cast("dict[str, object]", value)


def _dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast("dict[str, object]", v) for v in cast("list[object]", value) if isinstance(v, dict)]


def _with_content(content: str, kind: str) -> str:
    # pvesm set --content replaces the whole list, so the fix keeps what the storage already holds.
    return ",".join([*(c for c in content.split(",") if c), kind])


def _backup_fix(job: dict[str, object], ours: set[int]) -> str:
    """The command that keeps one enabled backup job off the runner VMs; empty when it already is."""
    if job.get("enabled") == 0:
        return ""
    command = f"pvesh set /cluster/backup/{job.get('id', '?')}"
    listed = _vmid_set(job.get("vmid"))
    if listed & ours:
        remaining = listed - ours
        return f"{command} --vmid {_ids(remaining)}" if remaining else f"{command} --enabled 0"
    excluded = _vmid_set(job.get("exclude"))
    if job.get("all") == 1 and ours - excluded:
        return f"{command} --exclude {_ids(excluded | ours)}"
    return ""


def _ids(vmids: set[int]) -> str:
    return ",".join(str(v) for v in sorted(vmids))


def _vmid_set(value: object) -> set[int]:
    return {int(part) for part in str(value or "").split(",") if part.strip().isdigit()}
