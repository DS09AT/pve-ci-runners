"""The two outside systems the controller drives, as the narrow interfaces it depends on."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pve_ci_runners.actions import QueuedJob, Registration
from pve_ci_runners.capacity import HostMemory
from pve_ci_runners.pve import VmInfo
from pve_ci_runners.state import VerifiedJob


class Hypervisor(Protocol):
    """The Proxmox VE node."""

    def vms(self) -> dict[int, VmInfo]:
        """Every VM on the node."""
        ...

    def config(self, vmid: int) -> dict[str, object]:
        """One VM's configuration."""
        ...

    def host_memory(self, excluded: Mapping[int, str]) -> HostMemory:
        """Physical memory and the memory configured for every guest, running or not.

        A VM is left out only if its VMID maps to its name in `excluded` and it carries our tag.
        """
        ...

    def set_description(self, vmid: int, text: str) -> None:
        """Stores a VM's state."""
        ...

    def clone(self, template: int, vmid: int, name: str) -> None:
        """Creates a linked clone."""
        ...

    def configure(self, vmid: int, options: Sequence[tuple[str, str]]) -> None:
        """Sets VM options."""
        ...

    def limit_disk(self, vmid: int, disk: str, limits: Sequence[tuple[str, int]]) -> None:
        """Adds IO limits to a disk."""
        ...

    def write_firewall(self, vmid: int, text: str) -> None:
        """Replaces a VM's firewall rules."""
        ...

    def start(self, vmid: int) -> None:
        """Boots a VM."""
        ...

    def protect(self, vmid: int) -> None:
        """Makes a running VM the kernel's first out-of-memory victim."""
        ...

    def destroy(self, vmid: int) -> None:
        """Removes a VM."""
        ...

    def attach(
        self, vmid: int, disk: str, device: str, serial: str, *, limits: Sequence[tuple[str, int]]
    ) -> None:
        """Hot-plugs a block device with IO limits."""
        ...

    def detach(self, vmid: int, disk: str) -> None:
        """Removes a hot-plugged disk."""
        ...

    def host_addresses(self) -> tuple[str, ...]:
        """The node's own global addresses."""
        ...

    def host_networks(self) -> tuple[str, ...]:
        """The node's own addresses and the networks they sit on, as CIDRs."""
        ...

    def agent_ready(self, vmid: int) -> bool:
        """Whether the guest agent answers."""
        ...

    def guest(self, vmid: int, script: str, *, stdin: str | None = None, timeout: int = 120) -> str:
        """Runs a script in the guest."""
        ...


class TemplateHypervisor(Hypervisor, Protocol):
    """The node, with the operations a template build needs on top of a runner VM's."""

    def create(self, vmid: int, options: Sequence[tuple[str, str]]) -> None:
        """Creates a VM."""
        ...

    def resize(self, vmid: int, disk: str, size: str) -> None:
        """Grows a disk."""
        ...

    def shutdown(self, vmid: int, timeout: int) -> None:
        """Shuts a VM down and waits until it is off."""
        ...

    def make_template(self, vmid: int) -> None:
        """Turns a VM into a template."""
        ...

    def snippet_path(self, volume: str) -> Path:
        """The file behind a snippet volume."""
        ...

    def firewall_enabled(self) -> bool:
        """Whether the datacenter firewall is on."""
        ...


class DiskSpace(Protocol):
    """The free space where runner VMs and job caches write."""

    def available(self) -> int | None:
        """Bytes one more write could still use, or None when nothing there can be measured."""
        ...


class GitHubActions(Protocol):
    """The GitHub Actions operations of one App installation."""

    def repositories(self) -> dict[str, str]:
        """Served repositories and their default branches."""
        ...

    def queued_jobs(self, repository: str) -> list[QueuedJob]:
        """Jobs waiting for a runner."""
        ...

    def busy_runners(self, repository: str) -> frozenset[str]:
        """Runners running a job right now."""
        ...

    def register(self, repository: str, name: str, labels: tuple[str, ...]) -> Registration:
        """A single-use runner registration."""
        ...

    def verify(self, repository: str, run_id: int, runner: str, *, running: bool) -> VerifiedJob | None:
        """The job a runner serves; with `running`, only while it is in progress."""
        ...

    def branch_head(self, repository: str, branch: str) -> str:
        """The commit a branch points to."""
        ...

    def conclusion(self, repository: str, run_id: int, runner: str) -> str | None:
        """The recorded conclusion of a runner's job."""
        ...

    def remove_runner(self, repository: str, name: str) -> None:
        """Deletes the registration of exactly this name."""
        ...

    def set_variable(self, repository: str, name: str, value: str) -> None:
        """Writes a repository Actions variable."""
        ...


class GitHubApp(Protocol):
    """The App installation itself, for checks of what it was granted."""

    def permissions(self) -> dict[str, str]:
        """The installation's permissions and their levels."""
        ...

    def get(self, path: str, *, conditional: bool = False) -> object:
        """One GET request."""
        ...
