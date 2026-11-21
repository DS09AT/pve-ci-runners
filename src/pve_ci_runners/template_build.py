"""Builds a runner template: a cloud image, provisioned through the guest agent, sealed as a template.

A build writes the template VMID that is not in use and records its marker only as the very last step,
so the controller never clones an interrupted build. The build VM sits behind the same firewall as a
runner VM; it is reached only through the guest agent.
"""

import contextlib
import fcntl
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from pve_ci_runners.capacity import HostMemory, vms_that_fit
from pve_ci_runners.config import Config, Slot
from pve_ci_runners.drain import drain, draining, memory_lock, release, reserve, resume
from pve_ci_runners.errors import PveCiError
from pve_ci_runners.firewall import render
from pve_ci_runners.guest import Guest, script, with_inputs
from pve_ci_runners.images import fetch_image
from pve_ci_runners.lifecycle import IsolationError
from pve_ci_runners.ports import TemplateHypervisor
from pve_ci_runners.pve import TAG
from pve_ci_runners.templates import TemplateMarker, current_template

_PROVISION = "provision"
_VERSIONED = frozenset({"node", "dotnet", "go", "python"})
_STEP_TIMEOUT = 1800
_AGENT_TIMEOUT = 1200
_SHUTDOWN_TIMEOUT = 300
_POLL = 10.0
_ROOM_POLL = 30.0
_ROOM_TIMEOUT = 7200
_NO_ROOM = "the host has no room for the build VM, so it was not started"
log = logging.getLogger("pve_ci_runners")


class TemplateBuilder:
    """Builds the next template of the configured pair."""

    def __init__(
        self,
        config: Config,
        host: TemplateHypervisor,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        fetch: Callable[[str, str, Path], Path] = fetch_image,
        script_root: Path = Path("/"),
    ) -> None:
        self._config = config
        self._host = host
        self._clock = clock
        self._sleep = sleep
        self._fetch = fetch
        self._script_root = script_root

    def build(self) -> tuple[int, str]:
        """Builds a template; returns its VMID and the runner version it carries."""
        state_dir = self._config.daemon.state_dir
        with _exclusive(state_dir / "template.lock"):
            if not self._host.firewall_enabled():
                raise PveCiError("the datacenter firewall is off, so the build VM would not be isolated")
            template = self._config.template
            scripts = [_read_script(path, self._script_root) for path in template.scripts]
            image = self._fetch(template.image_url, template.image_sha256, state_dir / "images")
            vmid = self._target()
            snippet = self._host.snippet_path(self._snippet(vmid))
            snippet.write_text(script("user-data.yaml", _PROVISION), encoding="utf-8")
            try:
                with self._room():
                    version = self._build_vm(vmid, image, scripts)
            finally:
                snippet.unlink(missing_ok=True)
            return vmid, version

    @contextmanager
    def _room(self) -> Generator[None]:
        """Holds memory for the build VM, and drains the runners until the host has that much to spare.

        The reservation keeps the controller from starting runner VMs into the memory the build needs;
        the host's other guests never give way to a build.
        """
        state_dir = self._config.daemon.state_dir
        reserve(state_dir, self._config.template.memory_mb)
        drained = False
        try:
            deadline = self._clock() + _ROOM_TIMEOUT
            while not self._fits():
                if not drained and not draining(state_dir):
                    log.info("draining the runners to make room for the build VM")
                    drain(state_dir)
                    drained = True
                if self._clock() > deadline:
                    raise PveCiError(_NO_ROOM)
                self._sleep(_ROOM_POLL)
            yield
        finally:
            if drained:
                resume(state_dir)
            release(state_dir)

    def _fits(self) -> bool:
        capacity, slots = self._config.capacity, {s.vmid for s in self._config.slots}
        runners = [info for vmid, info in self._host.vms().items() if vmid in slots]
        host = self._host.host_memory()
        # A slot VM that is not running yet is about to boot; its memory is already promised.
        booting = sum(info.memory_mb for info in runners if not info.running)
        return (
            vms_that_fit(
                HostMemory(host.total_mb, host.committed_mb + booting),
                reserve_mb=capacity.host_reserve_mb,
                budget_left_mb=capacity.memory_mb - sum(info.memory_mb for info in runners),
                vm_mb=self._config.template.memory_mb,
            )
            >= 1
        )

    def _build_vm(self, vmid: int, image: Path, scripts: list[str]) -> str:
        self._create(vmid, image)
        try:
            self._boot(vmid)
            version = self._provision(vmid, scripts)
            self._seal(vmid, version)
        except BaseException:
            # A failed build leaves nothing behind that could be mistaken for a template. Only a VM this
            # build created is removed: a VMID that qm create refused may belong to another VM.
            with contextlib.suppress(PveCiError):
                self._host.destroy(vmid)
            raise
        return version

    def _name(self, vmid: int) -> str:
        return f"{self._config.daemon.name_prefix}template-{vmid}"

    def _snippet(self, vmid: int) -> str:
        return f"{self._config.template.snippet_storage}:snippets/pve-ci-runners-build-{vmid}.yaml"

    def _target(self) -> int:
        """The VMID of the pair that is not the current template, freed of an earlier build."""
        vmids = self._config.template.vmids
        vms = self._host.vms()
        current = current_template(vmids, vms, lambda v: str(self._host.config(v).get("description", "")))
        vmid = vmids[1] if current is not None and current[0] == vmids[0] else vmids[0]
        info = vms.get(vmid)
        if info is not None:
            if TAG not in info.tags or info.name != self._name(vmid):
                raise PveCiError(f"VMID {vmid} holds a VM that pve-ci-runners did not build; free it first")
            log.info("removing the earlier template %s", vmid)
            self._host.destroy(vmid)
        return vmid

    def _create(self, vmid: int, image: Path) -> None:
        template, network = self._config.template, self._config.network
        storage = template.storage
        log.info("creating the build VM %s from %s", vmid, image.name)
        self._host.create(
            vmid,
            [
                ("name", self._name(vmid)),
                ("tags", TAG),
                ("machine", "q35"),
                ("bios", "ovmf"),
                ("efidisk0", f"{storage}:1,efitype=4m,ms-cert=2023k,pre-enrolled-keys=1"),
                ("scsihw", "virtio-scsi-single"),
                ("scsi0", f"{storage}:0,import-from={image},discard=on,iothread=1,ssd=1"),
                ("boot", "order=scsi0"),
                ("serial0", "socket"),
                ("vga", "serial0"),
                ("agent", "enabled=1"),
                ("ostype", "l26"),
                ("cpu", "host"),
                ("cores", str(template.cores)),
                ("memory", str(template.memory_mb)),
                ("balloon", "0"),
                ("net0", network.nic),
                ("ide2", f"{storage}:cloudinit"),
                ("cicustom", f"user={self._snippet(vmid)}"),
                ("ipconfig0", f"ip={template.address}/{network.prefix},gw={network.gateway}"),
                ("nameserver", " ".join(str(d) for d in network.dns)),
            ],
        )

    def _boot(self, vmid: int) -> None:
        """Locks the build VM down, boots it and proves its isolation before any provisioning step runs."""
        template = self._config.template
        # qm create refuses a VMID in use anywhere in the cluster, so the firewall file written after it
        # cannot replace another VM's rules; the VM still boots only once the file exists.
        self._host.write_firewall(vmid, render(Slot(vmid, template.address), self._config.firewall))
        self._host.resize(vmid, "scsi0", f"{template.disk_gb}G")
        # Checked again: runner VMs may have started while the image was imported.
        with memory_lock(self._config.daemon.state_dir):
            if not self._fits():
                raise PveCiError(_NO_ROOM)
            self._host.start(vmid)
        deadline = self._clock() + _AGENT_TIMEOUT
        while not self._host.agent_ready(vmid):
            if self._clock() > deadline:
                raise PveCiError(f"the build VM {vmid} did not start its guest agent in time")
            self._sleep(_POLL)
        guest = Guest(self._host, self._config.network, self._config.cache)
        if not guest.isolated(vmid, self._host.host_addresses()):
            raise IsolationError(
                f"the build VM {vmid} reached the hypervisor; check the datacenter and VM firewall"
            )

    def _provision(self, vmid: int, scripts: list[str]) -> str:
        template = self._config.template
        steps: list[tuple[str, dict[str, str]]] = [("base", {})] + [
            (c, {"VERSION": template.version(c)} if c in _VERSIONED else {}) for c in template.components
        ]
        for name, inputs in steps:
            log.info("build VM %s: %s", vmid, name)
            self._step(vmid, with_inputs(name, inputs, _PROVISION))
        for path, text in zip(template.scripts, scripts, strict=True):
            log.info("build VM %s: %s", vmid, path)
            self._step(vmid, text)
        report = self._step(vmid, with_inputs("finish", {}, _PROVISION))
        lines = report.strip().splitlines()
        version = lines[-1].strip() if lines else ""
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
            raise PveCiError(f"the build VM {vmid} reports no runner version")
        return version

    def _step(self, vmid: int, text: str) -> str:
        # A build VM restarted outside the build runs in a new QEMU process with the default priority.
        self._host.protect(vmid)
        return self._host.guest(vmid, text, timeout=_STEP_TIMEOUT)

    def _seal(self, vmid: int, version: str) -> None:
        self._host.shutdown(vmid, _SHUTDOWN_TIMEOUT)
        self._host.configure(vmid, [("delete", "ide2,cicustom,ipconfig0,nameserver")])
        self._host.make_template(vmid)
        self._host.set_description(vmid, TemplateMarker(int(self._clock()), version).encode())
        log.info("template %s is ready with runner %s", vmid, version)


def _read_script(path: object, root: Path) -> str:
    """An operator's provisioning script; it runs as root in the template, so only root may change it.

    Every directory from `root` down must be root's alone as well: whoever can write one of them could
    swap the file between the check and the read.
    """
    file = Path(str(path))
    try:
        for directory in reversed(file.parents):
            if directory.is_relative_to(root) and not _private(os.lstat(directory), stat.S_ISDIR):
                raise PveCiError(
                    f"{directory}: must be a directory owned by root and writable by no one else"
                )
        descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise PveCiError(f"{file}: cannot be read") from None
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        if not _private(os.fstat(handle.fileno()), stat.S_ISREG):
            raise PveCiError(f"{file}: must be a regular file owned by root and writable by no one else")
        return handle.read()


def _private(info: os.stat_result, kind: Callable[[int], bool]) -> bool:
    return kind(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022


@contextmanager
def _exclusive(lock: Path) -> Generator[None]:
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PveCiError("another template build is running") from None
        yield
