"""The life of one runner VM: created, warmed up, assigned, given its cache, finished or retired."""

import logging
import re
import time
import uuid
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from pve_ci_runners.cache import CacheBackend, cache_key
from pve_ci_runners.config import Config, Profile, Slot
from pve_ci_runners.errors import PveCiError
from pve_ci_runners.firewall import render
from pve_ci_runners.guest import CACHE_SERIAL, Guest, payload_version
from pve_ci_runners.ports import GitHubActions, Hypervisor
from pve_ci_runners.promotion import may_write_back, worth_keeping
from pve_ci_runners.pve import TAG
from pve_ci_runners.state import CacheLease, VmState

CACHE_DISK = "scsi1"
_RESULT_GRACE = 180
_RUNNER_SUFFIX = 6
_KEPT_LOG_BYTES = 2**20
log = logging.getLogger("pve_ci_runners")


class IsolationError(PveCiError):
    """A runner VM could reach the hypervisor; no job may run until an operator fixed the firewall."""


class Lifecycle:
    """Moves VMs through their phases; every step stores the new state on the VM first."""

    def __init__(
        self,
        config: Config,
        hypervisor: Hypervisor,
        github: GitHubActions,
        cache: CacheBackend,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._pve = hypervisor
        self._github = github
        self._cache = cache
        self._guest = Guest(hypervisor, config.network, config.cache)
        self._clock = clock

    def name(self, vmid: int) -> str:
        """The VM name of a slot."""
        return f"{self._config.daemon.name_prefix}{vmid}"

    def owns_runner(self, vmid: int, state: VmState) -> bool:
        """Whether the state names no runner or one this controller would have named for the VM."""
        pattern = re.escape(self.name(vmid)) + f"-[0-9a-f]{{{_RUNNER_SUFFIX}}}"
        return state.runner is None or re.fullmatch(pattern, state.runner) is not None

    def create(self, slot: Slot, profile: Profile, template: int) -> None:
        """Clones the template, locks the VM down and boots it."""
        self._pve.clone(template, slot.vmid, self.name(slot.vmid))
        # The clone inherited the template's firewall file; the slot's own rules replace it before boot.
        self._pve.write_firewall(slot.vmid, render(slot, self._config.firewall))
        state = VmState(
            phase="booting", since=int(self._clock()), profile=profile.name, payload=payload_version()
        )
        self._pve.configure(slot.vmid, self._vm_options(profile, state))
        if profile.disk_limits:
            self._pve.limit_disk(slot.vmid, "scsi0", profile.disk_limits)
        self._pve.start(slot.vmid)
        log.info("booting %s for profile %s", self.name(slot.vmid), profile.name)

    def warm_up(self, slot: Slot, state: VmState) -> VmState | None:
        """Configures a booted VM and proves its isolation; None while the guest agent is not up yet."""
        if not self._pve.agent_ready(slot.vmid):
            return None
        self._guest.prepare(slot.vmid, slot, self.name(slot.vmid))
        if not self._guest.isolated(slot.vmid, self._pve.host_addresses()):
            try:
                self._pve.destroy(slot.vmid)
            except PveCiError:
                # The breach must still stop all new work, even when the VM cannot be removed.
                log.exception("VM %s reached the hypervisor and could not be removed", slot.vmid)
            raise IsolationError(
                f"VM {slot.vmid} reached the hypervisor; check the datacenter and VM firewall"
            )
        return self._save(slot.vmid, state.evolve(phase="warm", since=int(self._clock())))

    def assign(self, vmid: int, state: VmState, repository: str) -> VmState:
        """Registers a single-use runner for the repository and starts it in the warm VM."""
        profile = self._config.profile(state.profile)
        runner = f"{self.name(vmid)}-{uuid.uuid4().hex[:_RUNNER_SUFFIX]}"
        registration = self._github.register(repository, runner, profile.labels)
        assigned = self._save(
            vmid,
            state.evolve(
                phase="assigned",
                repository=repository,
                runner=runner,
                since=int(self._clock()),
            ),
        )
        self._guest.start(vmid, registration.encoded_config)
        log.info("VM %s runs %s for %s", vmid, runner, repository)
        return assigned

    def attach(self, vmid: int, state: VmState) -> VmState | None:
        """Gives a started job its cache; None while the job has not started.

        The guest only reports a run id. The cache is chosen from what GitHub says this runner serves,
        so a job cannot pick another workflow's or repository's cache by lying about itself.
        """
        run_id = self._guest.started_run(vmid)
        if run_id is None or state.repository is None or state.runner is None:
            return None
        state = self._save(vmid, state.evolve(job_started=int(self._clock())))
        try:
            return self._attach_verified(vmid, state, run_id)
        except PveCiError as err:
            log.warning("VM %s runs without a cache: %s", vmid, err)
            self._guest.release(vmid)
            return state

    def _attach_verified(self, vmid: int, state: VmState, run_id: int) -> VmState:
        repository, runner = state.repository or "", state.runner or ""
        verified = self._github.verify(repository, run_id, runner, running=True)
        if verified is None:
            raise PveCiError("GitHub does not show this runner running the reported run")
        state = self._save(vmid, state.evolve(verified=verified))
        key = cache_key(repository, verified.workflow, verified.job)
        attachment = self._cache.lease(key, vmid, runner=runner, run_id=verified.run_id)
        if attachment is None:
            self._guest.release(vmid)
            return state
        state = self._save(vmid, state.evolve(cache=attachment.lease))
        self._pve.attach(vmid, CACHE_DISK, attachment.device, CACHE_SERIAL)
        self._guest.attach(vmid)
        log.info("VM %s got the cache %s", vmid, attachment.lease.key)
        return state

    def finish(self, vmid: int, state: VmState, repositories: Mapping[str, str]) -> bool:
        """Retires a VM whose runner exited; writes its cache back only after a trusted success.

        `repositories` maps the served repositories to their default branches.
        """
        default_branch = repositories.get(state.repository or "")
        conclusion = None
        if state.verified is not None and state.cache is not None and state.repository and state.runner:
            conclusion = self._github.conclusion(state.repository, state.verified.run_id, state.runner)
            now = int(self._clock())
            if conclusion is None and (state.done_at is None or now - state.done_at < _RESULT_GRACE):
                if state.done_at is None:
                    self._save(vmid, state.evolve(done_at=now))
                return False
        if conclusion != "success":
            self._keep_logs(vmid, state)
        keep = self._should_keep(vmid, state, conclusion, default_branch) and self._tidied(vmid)
        self._remove(vmid, state, repositories, write_back=keep)
        return True

    def _tidied(self, vmid: int) -> bool:
        try:
            self._guest.tidy(vmid)
        except PveCiError as err:
            log.warning("VM %s: cache not tidied, not written back: %s", vmid, err)
            return False
        return True

    def finished(self, vmid: int) -> bool:
        """Whether the VM's runner has exited."""
        return self._guest.finished(vmid)

    def outlived_job(self, vmid: int, state: VmState) -> bool:
        """Whether GitHub recorded the end of the job a grace period ago, although the guest never did.

        The caller retires such a VM without writing its cache back, because the guest never confirmed
        that it stopped writing.
        """
        if state.job_started is None or state.verified is None or not state.repository or not state.runner:
            return False
        if self._github.conclusion(state.repository, state.verified.run_id, state.runner) is None:
            return False
        now = int(self._clock())
        if state.done_at is None:
            self._save(vmid, state.evolve(done_at=now))
            return False
        return now - state.done_at >= _RESULT_GRACE

    def retire(self, vmid: int, state: VmState, served: Collection[str]) -> None:
        """Removes a VM without writing anything back."""
        self._remove(vmid, state, served, write_back=False)
        log.info("retired %s", state.runner or self.name(vmid))

    def _should_keep(
        self, vmid: int, state: VmState, conclusion: str | None, default_branch: str | None
    ) -> bool:
        if state.cache is None or default_branch is None or conclusion != "success":
            return False
        try:
            trusted = self._trusted(state, state.cache, conclusion, default_branch)
        except PveCiError as err:
            log.warning("VM %s: the run is unconfirmed, its cache is not written back: %s", vmid, err)
            return False
        return trusted and worth_keeping(
            self._cache.written_bytes(state.cache, vmid), min_write_mb=self._config.cache.min_write_mb
        )

    def _trusted(self, state: VmState, lease: CacheLease, conclusion: str, default_branch: str) -> bool:
        """Asks GitHub again; the stored state, which a Proxmox user can rewrite, only names the run."""
        if state.verified is None or not state.repository or not state.runner:
            return False
        fresh = self._github.verify(state.repository, state.verified.run_id, state.runner, running=False)
        if fresh is None or cache_key(state.repository, fresh.workflow, fresh.job) != lease.key:
            return False
        head = self._github.branch_head(state.repository, default_branch)
        return may_write_back(fresh, conclusion=conclusion, default_branch=default_branch, default_head=head)

    def _remove(self, vmid: int, state: VmState, served: Collection[str], *, write_back: bool) -> None:
        if state.cache is not None:
            # The disk is a host device the VM does not own; detached first, it survives the purge.
            self._pve.detach(vmid, CACHE_DISK)
        self._pve.destroy(vmid)
        written = False
        if state.cache is not None:
            written = write_back and self._written_back(vmid, state, state.cache)
            if not written:
                self._cache.drop(state.cache, vmid)
        self._remove_runner(vmid, state, served)
        log.info(
            "VM %s finished %s: cache %s", vmid, state.runner, "written back" if written else "discarded"
        )

    def _written_back(self, vmid: int, state: VmState, lease: CacheLease) -> bool:
        if state.verified is None or state.runner is None:
            return False
        try:
            return self._cache.write_back(lease, vmid, runner=state.runner, run_id=state.verified.run_id)
        except PveCiError as err:
            log.warning("VM %s: cache not written back: %s", vmid, err)
            return False

    def _remove_runner(self, vmid: int, state: VmState, served: Collection[str]) -> None:
        """Deletes a registration that never took its job; JIT runners remove themselves after one.

        The description names the repository and runner, so only this VM's own runner name in a served
        repository is ever looked up and deleted.
        """
        repository, runner = state.repository, state.runner
        if (
            repository is None
            or runner is None
            or repository not in served
            or not self.owns_runner(vmid, state)
        ):
            return
        try:
            self._github.remove_runner(repository, runner)
        except PveCiError as err:
            log.info("runner %s not removed, it expires on its own: %s", state.runner, err)

    def _keep_logs(self, vmid: int, state: VmState) -> None:
        try:
            text = self._guest.logs(vmid)
        except PveCiError as err:
            text = f"logs unavailable: {err}"
        log_dir = self._config.daemon.log_dir
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(log_dir, f"{state.runner or vmid}.log")
        path.write_bytes(text.encode("utf-8", errors="replace")[-_KEPT_LOG_BYTES:])
        path.chmod(0o600)

    def _save(self, vmid: int, state: VmState) -> VmState:
        self._pve.set_description(vmid, state.encode())
        return state

    def _vm_options(self, profile: Profile, state: VmState) -> list[tuple[str, str]]:
        return [
            ("onboot", "0"),
            ("tags", TAG),
            ("cores", str(profile.cores)),
            ("memory", str(profile.memory_mb)),
            ("balloon", "0"),
            ("net0", self._config.network.nic),
            *profile.qm_options,
            ("description", state.encode()),
        ]
