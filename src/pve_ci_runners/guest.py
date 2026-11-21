"""What the controller does inside a runner VM, through the guest agent only.

The scripts ship with the package and are pushed on every call, so a controller upgrade never needs a new
template. Parameters reach a script as shell-quoted variable assignments in front of it, after they were
validated by the configuration; anything a guest prints back is parsed here as untrusted input.
"""

import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Protocol

from pve_ci_runners.config import CacheConfig, NetworkConfig, Slot
from pve_ci_runners.errors import GuestError

CACHE_SERIAL = "cicache"
_SCRIPTS = ("prepare", "probe", "start", "report", "finished", "attach", "release", "tidy", "logs")
_MAX_RUN_ID = 2**53
_MAX_REPORT = 200
_ISOLATION_BREACH = 3


class GuestRunner(Protocol):
    """Runs a bash script inside a VM."""

    def guest(self, vmid: int, script: str, *, stdin: str | None = None, timeout: int = 120) -> str:
        """Runs the script and returns its standard output."""
        ...


@cache
def script(name: str) -> str:
    """The text of one shipped guest script."""
    return resources.files("pve_ci_runners").joinpath("guest", f"{name}.sh").read_text(encoding="utf-8")


@cache
def payload_version() -> str:
    """A digest of every guest script; warm VMs prepared with another version are replaced."""
    digest = hashlib.sha256()
    for name in _SCRIPTS:
        digest.update(script(name).encode())
    return digest.hexdigest()[:16]


def with_inputs(name: str, inputs: Mapping[str, str]) -> str:
    """The script with its inputs assigned in front of it, each value shell-quoted."""
    for key in inputs:
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None:
            raise ValueError(f"invalid input name {key!r}")
    assignments = "".join(f"{key}={shlex.quote(value)}\n" for key, value in sorted(inputs.items()))
    return assignments + script(name)


def parse_run_id(report: str) -> int | None:
    """The run id from the job hook's report, or None when the guest printed nothing usable."""
    text = report.strip()
    if not text:
        return None
    if len(text.encode()) > _MAX_REPORT:
        raise GuestError("the job report is too long")
    try:
        data: object = json.loads(text)
    except (ValueError, RecursionError):
        raise GuestError("the job report is not JSON") from None
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if isinstance(run_id, bool) or not isinstance(run_id, int) or not 0 < run_id < _MAX_RUN_ID:
        raise GuestError("the job report names no run")
    return run_id


class Guest:
    """The guest-side steps of a runner VM's life."""

    def __init__(self, runner: GuestRunner, network: NetworkConfig, cache_config: CacheConfig) -> None:
        self._runner = runner
        self._network = network
        self._cache = cache_config

    def prepare(self, vmid: int, slot: Slot, hostname: str) -> None:
        """Network, hostname, runner unit and job hook."""
        inputs = {
            "ADDRESS": str(slot.address),
            "PREFIX": str(self._network.prefix),
            "GATEWAY": str(self._network.gateway),
            "DNS": " ".join(str(d) for d in self._network.dns),
            "HOSTNAME": hostname,
        }
        self._runner.guest(vmid, with_inputs("prepare", inputs))

    def isolated(self, vmid: int, host_addresses: tuple[str, ...]) -> bool:
        """Whether a probe from inside the VM fails to reach the hypervisor on any of its addresses."""
        inputs = {"GATEWAY": str(self._network.gateway), "HOST_ADDRESSES": " ".join(host_addresses)}
        try:
            self._runner.guest(vmid, with_inputs("probe", inputs), timeout=30)
        except GuestError as err:
            if err.exit_code == _ISOLATION_BREACH:
                return False
            raise
        return True

    def start(self, vmid: int, encoded_config: str) -> None:
        """Hands the single-use configuration to the runner and starts it."""
        self._runner.guest(vmid, script("start"), stdin=encoded_config)

    def started_run(self, vmid: int) -> int | None:
        """The run the runner reports to have taken, once its job started."""
        return parse_run_id(self._runner.guest(vmid, script("report"), timeout=30))

    def finished(self, vmid: int) -> bool:
        """Whether the runner has exited."""
        return bool(self._runner.guest(vmid, script("finished"), timeout=30).strip())

    def attach(self, vmid: int) -> None:
        """Mounts the hot-plugged cache disk and lets the waiting job continue."""
        mounts = "\n".join(f"{m.directory} {m.path} {m.owner}" for m in self._cache.mounts)
        self._runner.guest(vmid, with_inputs("attach", {"SERIAL": CACHE_SERIAL, "MOUNTS": mounts}))

    def release(self, vmid: int) -> None:
        """Lets the job continue without a cache."""
        self._runner.guest(vmid, script("release"), timeout=60)

    def tidy(self, vmid: int) -> None:
        """Bounds the cache and unmounts it cleanly before it is written back."""
        inputs = {
            "UNUSED_DAYS": str(self._cache.unused_days),
            "DOCKER_KEEP_GB": str(self._cache.docker_keep_storage_gb),
            "WIPE_ABOVE_MB": str(self._cache.wipe_above_mb),
            "MOUNT_PATHS": "\n".join(str(m.path) for m in self._cache.mounts),
        }
        self._runner.guest(vmid, with_inputs("tidy", inputs), timeout=600)

    def logs(self, vmid: int) -> str:
        """The VM's journal and kernel log."""
        return self._runner.guest(vmid, script("logs"), timeout=60)
