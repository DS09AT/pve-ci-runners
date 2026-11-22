"""Removes what pve-ci-runners created on this node, and nothing else.

A VM is removed only at a configured slot or template VMID, with our tag and exactly the name
pve-ci-runners gives a VM there, so a foreign guest that took one of those VMIDs is never touched. Every
step is safe to repeat: what is already gone counts as removed, and a failed step is reported while the
others still run.
"""

import logging
import time
from collections.abc import Callable, Collection
from functools import partial

from pve_ci_runners.cache import CacheBackend
from pve_ci_runners.config import Config
from pve_ci_runners.drain import drain, release
from pve_ci_runners.errors import PveCiError
from pve_ci_runners.lifecycle import Lifecycle
from pve_ci_runners.ports import GitHubActions, TemplateHypervisor
from pve_ci_runners.pve import is_ours
from pve_ci_runners.state import VmState, decode
from pve_ci_runners.template_build import template_lock
from pve_ci_runners.templates import build_snippet, build_vm_name

log = logging.getLogger("pve_ci_runners")


class Teardown:
    """Drains the node, waits for running jobs, then removes the runner VMs, templates and registrations."""

    def __init__(
        self,
        config: Config,
        host: TemplateHypervisor,
        github: GitHubActions,
        cache: CacheBackend,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._host = host
        self._github = github
        self._cache = cache
        self._clock = clock
        self._sleep = sleep
        self._lifecycle = Lifecycle(config, host, github, cache, clock=clock)

    def run(self, *, purge_cache: bool) -> list[str]:
        """Tears the node down and leaves it drained; returns what failed, nothing when all is gone."""
        state_dir = self._config.daemon.state_dir
        drain(state_dir)
        with template_lock(state_dir):
            self._wait()
            failures = self._remove_vms()
            for vmid in self._config.template.vmids:
                failures += _attempt(f"build snippet {vmid}", partial(self._remove_snippet, vmid))
            if purge_cache:
                failures += _attempt("cache", self._cache.purge)
            # No build runs while the lock is held, so any reservation is a leftover.
            release(state_dir)
        return failures

    def _wait(self) -> None:
        while any(self._job_runs(vmid, s) for vmid, s in self._owned().items() if s is not None):
            log.info("waiting for running jobs to finish")
            self._sleep(self._config.daemon.poll_seconds)

    def _job_runs(self, vmid: int, state: VmState) -> bool:
        """Whether a VM may still run a job.

        Past `daemon.max_job_seconds` a job counts as done, as it does for the controller, so the wait is
        bounded; a guest that cannot answer counts as busy until then.
        """
        limit = self._config.daemon.max_job_seconds
        if state.phase != "assigned" or not state.running or self._clock() - state.since > limit:
            return False
        try:
            return not self._lifecycle.finished(vmid)
        except PveCiError:
            return True

    def _owned(self) -> dict[int, VmState | None]:
        """Our VMs with their state, slots first: a template can only go after its linked clones."""
        names = {s.vmid: self._lifecycle.name(s.vmid) for s in self._config.slots}
        names |= {v: build_vm_name(self._config.daemon.name_prefix, v) for v in self._config.template.vmids}
        infos = self._host.vms()
        owned: dict[int, VmState | None] = {}
        for vmid, info in infos.items():
            if is_ours(vmid, info.name, info.tags, names):
                description = str(self._host.config(vmid).get("description", ""))
                owned[vmid] = decode(description, running=info.running)
        return {vmid: owned[vmid] for vmid in names if vmid in owned}

    def _remove_vms(self) -> list[str]:
        owned = self._owned()
        served, failures = self._served(owned.values())
        for vmid, state in owned.items():
            if state is not None and state.runner is not None:
                failures += _attempt(
                    f"runner of VM {vmid}", partial(self._remove_runner, vmid, state, served)
                )
            failures += _attempt(f"VM {vmid}", partial(self._remove_vm, vmid, state))
        return failures

    def _served(self, states: Collection[VmState | None]) -> tuple[Collection[str], list[str]]:
        if not any(s is not None and s.runner is not None for s in states):
            return (), []
        try:
            return self._github.repositories(), []
        except PveCiError as err:
            return (), [f"runner registrations not removed, served repositories unknown: {err}"]

    def _remove_runner(self, vmid: int, state: VmState, served: Collection[str]) -> None:
        """Deletes the VM's registration by its exact name, before the VM, so a failure can be retried.

        Like the controller, only a name this node gives a runner, in a served repository, is deleted: the
        description that names it can be rewritten by any Proxmox user with VM.Config rights.
        """
        repository, runner = state.repository or "", state.runner or ""
        if repository not in served or not self._lifecycle.owns_runner(vmid, state):
            log.warning("VM %s names a runner this node did not register; not removed", vmid)
            return
        self._github.remove_runner(repository, runner)

    def _remove_vm(self, vmid: int, state: VmState | None) -> None:
        if state is None:
            self._host.destroy(vmid)
        else:
            # The registration went first, where a failure counts; `retire` would swallow one, so it gets
            # no served repository.
            self._lifecycle.retire(vmid, state, ())
        log.info("removed VM %s", vmid)

    def _remove_snippet(self, vmid: int) -> None:
        self._host.snippet_path(build_snippet(self._config.template.snippet_storage, vmid)).unlink(
            missing_ok=True
        )


def _attempt(what: str, action: Callable[[], object]) -> list[str]:
    try:
        action()
    except (PveCiError, OSError) as err:
        return [f"{what}: {err}"]
    return []
