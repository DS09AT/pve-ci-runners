"""Proxmox VE operations on this node, through `qm` and `pvesh` with argument lists only."""

import ipaddress
import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pve_ci_runners.capacity import HostMemory
from pve_ci_runners.errors import CommandError, GuestError
from pve_ci_runners.proc import Commands, output, succeeds

TAG = "pve-ci-runners"
_MB = 2**20
_GUEST_GRACE = 30
_LIMIT_KEYS = ("mbps_rd", "mbps_wr", "iops_rd", "iops_wr")


@dataclass(frozen=True)
class VmInfo:
    """What the node list says about one VM."""

    vmid: int
    name: str
    running: bool
    memory_mb: int
    template: bool
    tags: frozenset[str]


class Pve:
    """The node this controller runs on."""

    def __init__(
        self,
        commands: Commands,
        *,
        node: str | None = None,
        firewall_dir: Path = Path("/etc/pve/firewall"),
        run_dir: Path = Path("/var/run/qemu-server"),
        proc_dir: Path = Path("/proc"),
    ) -> None:
        self._commands = commands
        self.node = node or socket.gethostname().split(".")[0]
        self._firewall_dir = firewall_dir
        self._run_dir = run_dir
        self._proc_dir = proc_dir

    def vms(self) -> dict[int, VmInfo]:
        """Every VM on this node."""
        listing = self._json(["pvesh", "get", f"/nodes/{self.node}/qemu", "--output-format", "json"])
        result: dict[int, VmInfo] = {}
        for item in _dicts(listing):
            vmid = item.get("vmid")
            if isinstance(vmid, int):
                result[vmid] = VmInfo(
                    vmid=vmid,
                    name=str(item.get("name", "")),
                    running=item.get("status") == "running",
                    memory_mb=_int(item.get("maxmem")) // _MB,
                    template=item.get("template") in {1, "1"},
                    tags=frozenset(t for t in str(item.get("tags", "")).split(";") if t),
                )
        return result

    def config(self, vmid: int) -> dict[str, object]:
        """The current configuration of one VM."""
        value = self._json([
            "pvesh",
            "get",
            f"/nodes/{self.node}/qemu/{vmid}/config",
            "--output-format",
            "json",
        ])
        if not isinstance(value, dict):
            raise CommandError(f"VM {vmid} returned no configuration")
        return cast("dict[str, object]", value)

    def host_memory(self) -> HostMemory:
        """Physical memory and what the running guests of every kind are promised."""
        status = self._json(["pvesh", "get", f"/nodes/{self.node}/status", "--output-format", "json"])
        memory = _dicts([cast("dict[str, object]", status).get("memory")]) if isinstance(status, dict) else []
        total = _int(memory[0].get("total")) if memory else 0
        if total <= 0:
            raise CommandError("the node status reports no memory total")
        guests = [
            *_dicts(self._json(["pvesh", "get", f"/nodes/{self.node}/qemu", "--output-format", "json"])),
            *_dicts(self._json(["pvesh", "get", f"/nodes/{self.node}/lxc", "--output-format", "json"])),
        ]
        committed = sum(_int(g.get("maxmem")) for g in guests if g.get("status") == "running")
        return HostMemory(total_mb=total // _MB, committed_mb=committed // _MB)

    def set_description(self, vmid: int, text: str) -> None:
        """Stores the controller's state on the VM."""
        output(self._commands, ["qm", "set", str(vmid), "--description", text], timeout=60)

    def clone(self, template: int, vmid: int, name: str) -> None:
        """A linked clone of the template."""
        output(
            self._commands,
            ["qm", "clone", str(template), str(vmid), "--name", name, "--full", "0"],
            timeout=180,
        )

    def configure(self, vmid: int, options: Sequence[tuple[str, str]]) -> None:
        """Sets VM options; each option is passed as its own argument pair."""
        argv = ["qm", "set", str(vmid)]
        for key, value in options:
            argv += [f"--{key}", value]
        output(self._commands, argv, timeout=60)

    def create(self, vmid: int, options: Sequence[tuple[str, str]]) -> None:
        """A new VM; importing a disk image can take minutes."""
        argv = ["qm", "create", str(vmid)]
        for key, value in options:
            argv += [f"--{key}", value]
        output(self._commands, argv, timeout=1800)

    def resize(self, vmid: int, disk: str, size: str) -> None:
        """Grows a disk to the given size."""
        output(self._commands, ["qm", "disk", "resize", str(vmid), disk, size], timeout=300)

    def shutdown(self, vmid: int, timeout: int) -> None:
        """Shuts a VM down through ACPI and waits until it is off."""
        output(self._commands, ["qm", "shutdown", str(vmid), "--timeout", str(timeout)], timeout=timeout + 60)

    def make_template(self, vmid: int) -> None:
        """Turns a stopped VM into a template that linked clones can share."""
        output(self._commands, ["qm", "template", str(vmid)], timeout=600)

    def firewall_enabled(self) -> bool:
        """Whether the datacenter firewall is on; without it no VM firewall rule takes effect."""
        return datacenter_firewall_enabled(self._commands)

    def snippet_path(self, volume: str) -> Path:
        """Where a snippet volume such as `local:snippets/name.yaml` lives on this node."""
        path = Path(output(self._commands, ["pvesm", "path", volume], timeout=60).strip())
        if not path.is_absolute():
            raise CommandError(f"pvesm path {volume} returned no absolute path")
        return path

    def limit_disk(self, vmid: int, disk: str, limits: Sequence[tuple[str, int]]) -> None:
        """Adds IO limits to an existing disk, keeping its volume and other options."""
        current = str(self.config(vmid).get(disk, ""))
        if not current:
            raise CommandError(f"VM {vmid} has no disk {disk}")
        kept = [p for p in current.split(",") if p.split("=", 1)[0] not in _LIMIT_KEYS]
        self.configure(vmid, [(disk, ",".join([*kept, *(f"{k}={v}" for k, v in limits)]))])

    def write_firewall(self, vmid: int, text: str) -> None:
        """Replaces the VM's firewall rules; the cluster file system distributes the file."""
        (self._firewall_dir / f"{vmid}.fw").write_text(text, encoding="utf-8")

    def start(self, vmid: int) -> None:
        """Boots a VM that the kernel sacrifices first should the host ever run out of memory.

        The memory guard keeps the host's other guests safe; this is the layer behind it. A VM whose
        priority cannot be lowered is stopped again rather than left running unprotected. A failed or
        timed-out `qm start` may still have launched QEMU, so it is stopped as well.
        """
        try:
            output(self._commands, ["qm", "start", str(vmid)], timeout=180)
            self.protect(vmid)
        except CommandError as err:
            try:
                output(self._commands, ["qm", "stop", str(vmid), "--skiplock", "1"], timeout=180)
            except CommandError:
                raise CommandError(
                    f"VM {vmid} failed to start ({err}) and could not be stopped; it may run unprotected"
                ) from None
            raise CommandError(f"VM {vmid} failed to start and was stopped: {err}") from None

    def protect(self, vmid: int) -> None:
        """Makes a running VM the first process the kernel kills should the host run out of memory."""
        try:
            pid = int((self._run_dir / f"{vmid}.pid").read_text(encoding="utf-8").strip())
            score = self._proc_dir / str(pid) / "oom_score_adj"
            score.write_text("1000", encoding="utf-8")
            protected = score.read_text(encoding="utf-8").strip() == "1000"
        except (OSError, ValueError):
            protected = False
        if not protected:
            raise CommandError(f"VM {vmid}: its out-of-memory priority could not be set")

    def destroy(self, vmid: int) -> None:
        """Stops and removes a VM with its disks and firewall file."""
        self._commands.run(["qm", "stop", str(vmid), "--skiplock", "1"], timeout=180)
        output(self._commands, ["qm", "destroy", str(vmid), "--purge", "1"], timeout=300)

    def attach(self, vmid: int, disk: str, device: str, serial: str) -> None:
        """Hot-plugs a host block device; the guest finds it by its serial."""
        output(
            self._commands,
            ["qm", "set", str(vmid), f"--{disk}", f"{device},serial={serial},discard=on"],
            timeout=120,
        )

    def detach(self, vmid: int, disk: str) -> None:
        """Removes a hot-plugged disk without touching the device behind it."""
        self._commands.run(["qm", "set", str(vmid), "--delete", disk], timeout=120)

    def host_addresses(self) -> tuple[str, ...]:
        """Every global, non-loopback address of this node; an empty list is an error, never "none"."""
        found: list[str] = []
        for link in _dicts(self._json(["ip", "-j", "addr"])):
            for info in _dicts(link.get("addr_info")):
                address = _global_address(info)
                if address is not None and address not in found:
                    found.append(address)
        if not found:
            raise CommandError("ip addr listed no global address of this node")
        return tuple(found)

    def agent_ready(self, vmid: int) -> bool:
        """Whether the guest agent answers."""
        return succeeds(self._commands, ["qm", "agent", str(vmid), "ping"], timeout=30)

    def guest(self, vmid: int, script: str, *, stdin: str | None = None, timeout: int = 120) -> str:
        """Runs a bash script in the guest and returns its standard output.

        `qm guest exec` returns once its own timeout passes even if the command still runs, so a missing
        `exited` flag is a timeout, not a success.
        """
        argv = ["qm", "guest", "exec", str(vmid), "--timeout", str(timeout)]
        if stdin is not None:
            argv += ["--pass-stdin", "1"]
        try:
            raw = output(
                self._commands,
                [*argv, "--", "bash", "-c", script],
                stdin=stdin,
                timeout=timeout + _GUEST_GRACE,
            )
        except CommandError as err:
            raise GuestError(f"VM {vmid}: {err}") from None
        return _guest_output(_guest_result(raw, vmid), vmid, timeout)

    def _json(self, argv: list[str]) -> object:
        try:
            return json.loads(output(self._commands, argv, timeout=60))
        except ValueError:
            raise CommandError(f"{' '.join(argv[:3])} returned no JSON") from None


def _global_address(info: dict[str, object]) -> str | None:
    if info.get("scope") != "global":
        return None
    try:
        address = ipaddress.ip_address(str(info.get("local", "")))
    except ValueError:
        raise CommandError("ip addr listed an invalid address") from None
    return None if address.is_loopback else str(address)


def _guest_output(result: dict[str, object], vmid: int, timeout: int) -> str:
    if result.get("exited") != 1:
        raise GuestError(f"VM {vmid}: guest command did not finish within {timeout} s")
    code = result.get("exitcode")
    if code != 0:
        tail = _printable(str(result.get("err-data", ""))[-300:])
        raise GuestError(
            f"VM {vmid}: guest command exited {code}: {tail}", code if isinstance(code, int) else None
        )
    if result.get("out-truncated"):
        raise GuestError(f"VM {vmid}: guest output was truncated")
    return str(result.get("out-data", ""))


def datacenter_firewall_enabled(commands: Commands) -> bool:
    """Whether the datacenter firewall is on; without it no VM firewall rule takes effect."""
    try:
        options = json.loads(
            output(
                commands, ["pvesh", "get", "/cluster/firewall/options", "--output-format", "json"], timeout=60
            )
        )
    except ValueError:
        raise CommandError("pvesh get /cluster/firewall/options returned no JSON") from None
    return isinstance(options, dict) and cast("dict[str, object]", options).get("enable") == 1


def _guest_result(raw: str, vmid: int) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except ValueError:
        raise GuestError(f"VM {vmid}: guest agent returned no JSON") from None
    if not isinstance(value, dict):
        raise GuestError(f"VM {vmid}: guest agent returned no JSON object")
    return cast("dict[str, object]", value)


def _printable(text: str) -> str:
    # Guest output reaches the controller's log; escaped, it cannot forge lines or terminal sequences.
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


def _dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast("dict[str, object]", v) for v in cast("list[object]", value) if isinstance(v, dict)]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
