"""In-memory stand-ins for the hypervisor, GitHub and the cache, close enough to drive the controller."""

import fcntl
import json
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pve_ci_runners.actions import QueuedJob, Registration, RunSummary
from pve_ci_runners.cache import Attachment
from pve_ci_runners.capacity import HostMemory
from pve_ci_runners.errors import CommandError, GitHubError, GuestError
from pve_ci_runners.guest import script
from pve_ci_runners.proc import Completed
from pve_ci_runners.pve import TAG, VmInfo
from pve_ci_runners.state import CacheLease, VerifiedJob
from pve_ci_runners.templates import TemplateMarker

HEAD = "0" * 40


GUEST = ("probe", "report", "finished", "logs", "prepare", "start", "attach", "release", "tidy")
PROVISION = ("base", "docker", "gh", "node", "dotnet", "go", "python", "finish")


@dataclass
class FakeVm:
    name: str
    memory_mb: int = 2048
    running: bool = False
    template: bool = False
    tags: set[str] = field(default_factory=set)
    description: str = ""
    agent: bool = True
    isolated: bool = True
    run_id: int | None = None
    done: bool = False
    journal: str = "journal"
    silent: bool = False
    disks: dict[str, str] = field(default_factory=dict)
    options: dict[str, str] = field(default_factory=dict)


class FakeHost:
    def __init__(self, *, total_mb: int = 64000, other_guests_mb: int = 24000) -> None:
        self.machines: dict[int, FakeVm] = {}
        self.total_mb = total_mb
        self.other_guests_mb = other_guests_mb
        self.firewall: dict[int, str] = {}
        self.events: list[str] = []
        self.scripts: list[tuple[int, str]] = []
        self.undestroyable: set[int] = set()
        self.failing_steps: set[str] = set()
        self.runner_version = "2.340.1"
        self.snippet_dir = Path("/nonexistent")
        self.probes: list[str] = []
        self.addresses: tuple[str, ...] = ("192.168.100.1", "203.0.113.10")
        self.networks: tuple[str, ...] = (
            "192.168.100.1/32",
            "192.168.100.0/24",
            "203.0.113.10/32",
            "203.0.113.0/24",
        )
        self.datacenter_firewall = True
        self.uncreatable: set[int] = set()
        self.protected: set[int] = set()
        self.unprotectable: set[int] = set()
        self.disk_bytes: int | None = 2**50
        self.disk_error = False

    def available(self) -> int | None:
        """The host doubles as the disk port: free bytes where runner VMs write."""
        if self.disk_error:
            raise CommandError("zfs get exited 1: dataset is busy")
        return self.disk_bytes

    def add_template(self, vmid: int, built_at: int = 100) -> None:
        marker = TemplateMarker(built_at=built_at, runner_version="2.338.2").encode()
        self.machines[vmid] = FakeVm(name=f"tpl-{vmid}", template=True, description=marker)

    def vms(self) -> dict[int, VmInfo]:
        return {
            vmid: VmInfo(vmid, vm.name, vm.running, vm.memory_mb, vm.template, frozenset(vm.tags))
            for vmid, vm in self.machines.items()
        }

    def config(self, vmid: int) -> dict[str, object]:
        return {"description": self.machines[vmid].description, **self.machines[vmid].disks}

    def host_memory(self, excluded) -> HostMemory:
        guests = sum(
            vm.memory_mb
            for vmid, vm in self.machines.items()
            if not vm.template and not (excluded.get(vmid) == vm.name and TAG in vm.tags)
        )
        return HostMemory(self.total_mb, self.other_guests_mb + guests)

    def set_description(self, vmid: int, text: str) -> None:
        self.machines[vmid].description = text

    def clone(self, template: int, vmid: int, name: str) -> None:
        assert self.machines[template].template
        self.machines[vmid] = FakeVm(name=name, disks={"scsi0": "local-zfs:base-disk,iothread=1"})
        self.events.append(f"clone {vmid}")

    def configure(self, vmid: int, options) -> None:
        vm = self.machines[vmid]
        for key, value in options:
            vm.options[key] = value
            _apply_option(vm, key, value)

    def limit_disk(self, vmid: int, disk: str, limits) -> None:
        self.machines[vmid].disks[disk] += "".join(f",{k}={v}" for k, v in limits)

    def write_firewall(self, vmid: int, text: str) -> None:
        self.firewall[vmid] = text

    def start(self, vmid: int) -> None:
        assert vmid in self.firewall, "a VM must never boot before its firewall file is written"
        self.machines[vmid].running = True
        self.protected.add(vmid)
        self.events.append(f"start {vmid}")

    def protect(self, vmid: int) -> None:
        if vmid in self.unprotectable:
            raise CommandError(f"VM {vmid}: its out-of-memory priority could not be set")
        self.protected.add(vmid)

    def destroy(self, vmid: int) -> None:
        if vmid in self.undestroyable:
            raise CommandError(f"qm destroy {vmid} failed")
        self.machines.pop(vmid, None)
        self.firewall.pop(vmid, None)
        self.events.append(f"destroy {vmid}")

    def attach(self, vmid: int, disk: str, device: str, serial: str, *, limits) -> None:
        self.machines[vmid].disks[disk] = f"{device},serial={serial}" + "".join(
            f",{k}={v}" for k, v in limits
        )

    def detach(self, vmid: int, disk: str) -> None:
        self.machines[vmid].disks.pop(disk, None)

    def host_addresses(self) -> tuple[str, ...]:
        if not self.addresses:
            raise CommandError("ip addr listed no global address of this node")
        return self.addresses

    def host_networks(self) -> tuple[str, ...]:
        if not self.networks:
            raise CommandError("ip addr listed no address of this node")
        return self.networks

    def agent_ready(self, vmid: int) -> bool:
        return self.machines[vmid].agent

    def create(self, vmid: int, options) -> None:
        if vmid in self.uncreatable:
            raise CommandError(f"qm create {vmid} failed: VM {vmid} already exists on node 'pve2'")
        values = dict(options)
        self.machines[vmid] = FakeVm(name=values["name"], tags=set(values["tags"].split(";")), options=values)
        self.events.append(f"create {vmid}")

    def resize(self, vmid: int, disk: str, size: str) -> None:
        self.events.append(f"resize {vmid} {disk} {size}")

    def shutdown(self, vmid: int, timeout: int) -> None:
        self.machines[vmid].running = False
        self.events.append(f"shutdown {vmid}")

    def make_template(self, vmid: int) -> None:
        self.machines[vmid].template = True
        self.events.append(f"template {vmid}")

    def firewall_enabled(self) -> bool:
        return self.datacenter_firewall

    def snippet_path(self, volume: str) -> Path:
        return self.snippet_dir / volume.split("/", 1)[1]

    def guest(self, vmid: int, text: str, *, stdin: str | None = None, timeout: int = 120) -> str:
        vm = self.machines[vmid]
        built = next((n for n in PROVISION if text.endswith(script(f"{n}.sh", "provision"))), None)
        name = next((n for n in GUEST if text.endswith(script(f"{n}.sh"))), None)
        if built is not None or name is None:
            return self._build_step(vmid, built or "operator")
        self.scripts.append((vmid, name))
        if vm.silent:
            raise GuestError("guest agent not running")
        if name == "probe":
            self.probes.append(text)
            if not vm.isolated:
                raise GuestError("exited 3", 3)
        answers = {
            "report": "" if vm.run_id is None or vm.done else json.dumps({"run_id": vm.run_id}),
            "finished": "{}" if vm.done else "",
            "logs": vm.journal,
        }
        return answers.get(name, "")

    def _build_step(self, vmid: int, step: str) -> str:
        self.scripts.append((vmid, step))
        if step in self.failing_steps:
            raise GuestError(f"{step} failed", 1)
        return f"some output\n{self.runner_version}\n" if step == "finish" else ""


def _apply_option(vm: FakeVm, key: str, value: str) -> None:
    if key == "description":
        vm.description = value
    elif key == "tags":
        vm.tags = set(value.split(";"))
    elif key == "memory":
        vm.memory_mb = int(value)
    elif key.startswith("scsi"):
        vm.disks[key] = value


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: dict[str, str] = {"example-org/app": "main"}
        self.queued: dict[str, list[QueuedJob]] = {}
        self.busy: set[str] = set()
        self.runs: dict[int, VerifiedJob] = {}
        self.conclusions: dict[int, str] = {}
        self.heads: dict[str, str] = {"example-org/app": HEAD}
        self.registered: dict[str, str] = {}
        self.removed: list[str] = []
        self.variables: dict[tuple[str, str], str] = {}
        self.cancelled: list[int] = []
        self.reruns: list[int] = []

    def queue(
        self, repository: str, labels: tuple[str, ...] = ("self-hosted", "pve-ci"), count: int = 1
    ) -> None:
        self.queued.setdefault(repository, []).extend(
            QueuedJob(frozenset(labels), 900.0) for _ in range(count)
        )

    def repositories(self) -> dict[str, str]:
        return dict(self.repos)

    def queued_jobs(self, repository: str) -> list[QueuedJob]:
        return list(self.queued.get(repository, []))

    def busy_runners(self, repository: str) -> frozenset[str]:
        return frozenset(self.busy)

    def register(self, repository: str, name: str, labels: tuple[str, ...]) -> Registration:
        self.registered[name] = repository
        if self.queued.get(repository):
            self.queued[repository].pop()
        return Registration("ZW5jb2RlZA==")

    def verify(self, repository: str, run_id: int, runner: str, *, running: bool) -> VerifiedJob | None:
        if runner not in self.registered or (running and run_id in self.conclusions):
            return None
        return self.runs.get(run_id)

    def branch_head(self, repository: str, branch: str) -> str:
        if repository not in self.heads:
            raise GitHubError("branch not found", 404)
        return self.heads[repository]

    def conclusion(self, repository: str, run_id: int, runner: str) -> str | None:
        return self.conclusions.get(run_id)

    def remove_runner(self, repository: str, name: str) -> None:
        self.removed.append(name)

    def set_variable(self, repository: str, name: str, value: str) -> None:
        self.variables[(repository, name)] = value

    def summary(self, repository: str, run_id: int) -> RunSummary:
        return next(j.run for j in self.queued.get(repository, []) if j.run_id == run_id and j.run)

    def cancel(self, repository: str, run_id: int) -> None:
        self.cancelled.append(run_id)
        self.queued[repository] = [j for j in self.queued.get(repository, []) if j.run_id != run_id]

    def rerun_safely(self, repository: str, run_id: int, *, strict: bool) -> str | None:
        self.reruns.append(run_id)
        return None

    def run_conclusion(self, repository: str, run_id: int) -> str | None:
        return "cancelled" if run_id in self.cancelled else None


class FakeCache:
    def __init__(self) -> None:
        self.leases: list[CacheLease] = []
        self.written: list[str] = []
        self.dropped: list[str] = []
        self.bytes = 10 * 2**20
        self.leased_for: dict[str, tuple[str, int]] = {}
        self.purged = False

    def lease(self, key: str, vmid: int, *, runner: str, run_id: int) -> Attachment:
        lease = CacheLease(
            key, f"rpool/ci-cache/jobs/{key.replace('/', '.')}-{vmid}", f"rpool/ci-cache/repos/{key}@g1"
        )
        self.leases.append(lease)
        self.leased_for[lease.clone] = (runner, run_id)
        return Attachment(lease, f"/dev/zvol/{lease.clone}")

    def written_bytes(self, lease: CacheLease, vmid: int) -> int:
        return self.bytes

    def write_back(self, lease: CacheLease, vmid: int, *, runner: str, run_id: int) -> bool:
        if self.leased_for.get(lease.clone) != (runner, run_id):
            return False
        self.written.append(lease.key)
        return True

    def drop(self, lease: CacheLease, vmid: int) -> None:
        self.dropped.append(lease.key)

    def maintain(self, in_use: set[str]) -> list[str]:
        return []

    def purge(self) -> None:
        self.purged = True


class Script:
    """Answers host commands from a table keyed by their leading arguments."""

    def __init__(self, answers: dict[tuple[str, ...], Completed | list[Completed]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []

    def run(self, argv, *, stdin=None, timeout=300):
        argv = list(argv)
        self.calls.append(argv)
        self.stdin.append(stdin)
        for length in range(len(argv), 0, -1):
            answer = self.answers.get(tuple(argv[:length]))
            if answer is not None:
                return (
                    answer.pop(0)
                    if isinstance(answer, list) and len(answer) > 1
                    else (answer[0] if isinstance(answer, list) else answer)
                )
        return Completed(0, "", "")


def space(available: int, used: int = 0, quota: int = 0) -> Completed:
    """What `zfs get -Hp -o property,value available,used,quota` prints."""
    return Completed(0, f"available\t{available}\nused\t{used}\nquota\t{quota}\n", "")


def done(stdout: object = "", code: int = 0, stderr: str = "") -> Completed:
    return Completed(code, stdout if isinstance(stdout, str) else json.dumps(stdout), stderr)


def holds_memory_lock(state_dir: Path) -> bool:
    """Whether someone holds the lock that serialises memory checks with VM starts."""
    with (state_dir / "memory.lock").open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


class Background(Executor):
    """Holds submitted work until `run`, so a test decides when it ends; `inline` runs it at once."""

    def __init__(self, *, inline: bool = False) -> None:
        self.inline = inline
        self.pending: list[tuple[Future, partial]] = []

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future: Future = Future()
        self.pending.append((future, partial(fn, *args, **kwargs)))
        if self.inline:
            self.run()
        return future

    def run(self) -> None:
        while self.pending:
            future, work = self.pending.pop(0)
            try:
                future.set_result(work())
            except Exception as err:  # noqa: BLE001 - the future carries any failure to its reader
                future.set_exception(err)
