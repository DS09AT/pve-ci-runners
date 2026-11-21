"""One polling cycle: read the host and GitHub, decide, act.

Nothing is remembered between cycles that the host does not hold itself.
"""

import collections
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from pve_ci_runners.actions import QueuedJob
from pve_ci_runners.cache import CacheBackend
from pve_ci_runners.capacity import HostMemory, vms_that_fit
from pve_ci_runners.config import Config, Profile, Slot
from pve_ci_runners.drain import draining, memory_lock, reserved_mb
from pve_ci_runners.errors import PveCiError
from pve_ci_runners.guest import payload_version
from pve_ci_runners.lifecycle import IsolationError, Lifecycle
from pve_ci_runners.logs import prune_logs
from pve_ci_runners.overflow import Overflow
from pve_ci_runners.ports import GitHubActions, Hypervisor
from pve_ci_runners.pve import TAG, VmInfo
from pve_ci_runners.scheduler import Demand, Plan, ProfileLimits, Round, plan_all
from pve_ci_runners.state import VmState, decode
from pve_ci_runners.templates import current_template

_REPOSITORY_REFRESH = 600
_MAINTENANCE_INTERVAL = 3600
log = logging.getLogger("pve_ci_runners")


@dataclass
class _Demand:
    queued: dict[str, collections.Counter[str]] = field(default_factory=dict[str, collections.Counter[str]])
    oldest: dict[str, dict[str, float]] = field(default_factory=dict[str, dict[str, float]])


def profile_for(labels: frozenset[str], profiles: tuple[Profile, ...]) -> Profile | None:
    """The profile a job asks for: its match label is requested and it offers every requested label."""
    lowered = {label.lower() for label in labels}
    for profile in profiles:
        if profile.match.lower() in lowered and lowered <= {label.lower() for label in profile.labels}:
            return profile
    return None


class Controller:
    """Drives every slot VM of this host."""

    def __init__(
        self,
        config: Config,
        hypervisor: Hypervisor,
        github: GitHubActions,
        cache: CacheBackend,
        *,
        overflow: Overflow | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._pve = hypervisor
        self._github = github
        self._cache = cache
        self._overflow = overflow
        self._clock = clock
        self._lifecycle = Lifecycle(config, hypervisor, github, cache, clock=clock)
        self._slots = {s.vmid: s for s in config.slots}
        self._repositories: dict[str, str] = {}
        self._repositories_at = -float(_REPOSITORY_REFRESH)
        self._maintained_at = -float(_MAINTENANCE_INTERVAL)
        self.blocked: str | None = None

    def cycle(self) -> None:
        """Finishes what is done, plans, and starts what is needed."""
        now = self._clock()
        repositories = self._served_repositories(now)
        infos = self._pve.vms()
        states = self._advance(self._states(infos), repositories, now)
        demand = self._demand(repositories, now)
        busy = self._busy({s.repository for s in states.values() if s.repository})
        plans = self._plan(states, infos, demand, busy=busy, now=now)
        self._apply(plans, states, infos)
        self._update_overflow(plans, demand, now)
        if now - self._maintained_at >= _MAINTENANCE_INTERVAL:
            self._maintain(states)
            self._maintained_at = now

    def attach_pending(self) -> None:
        """Gives started jobs their cache between polls; a job waits for it before its first step."""
        infos = self._pve.vms()
        for vmid, state in self._states(infos).items():
            if state.phase != "assigned" or state.job_started is not None or not state.running:
                continue
            try:
                self._lifecycle.attach(vmid, state)
            except PveCiError as err:
                log.warning("VM %s: %s", vmid, err)

    def _served_repositories(self, now: float) -> dict[str, str]:
        if now - self._repositories_at >= _REPOSITORY_REFRESH:
            found = self._github.repositories()
            if found != self._repositories:
                log.info("serving %d private repositories", len(found))
            self._repositories, self._repositories_at = found, now
        return self._repositories

    def _states(self, infos: dict[int, VmInfo]) -> dict[int, VmState]:
        """Our slot VMs; a VM in a slot that does not carry our tag and name is never touched."""
        states: dict[int, VmState] = {}
        for vmid in self._slots:
            info = infos.get(vmid)
            if info is None or TAG not in info.tags or info.name != self._lifecycle.name(vmid):
                continue
            state = decode(str(self._pve.config(vmid).get("description", "")), running=info.running)
            if (
                state is None
                or state.profile not in {p.name for p in self._config.profiles}
                or not self._lifecycle.owns_runner(vmid, state)
            ):
                log.warning("VM %s carries no valid state; it is removed", vmid)
                self._pve.destroy(vmid)
                continue
            states[vmid] = state
        return states

    def _advance(
        self, states: dict[int, VmState], repositories: dict[str, str], now: float
    ) -> dict[int, VmState]:
        result: dict[int, VmState] = {}
        for vmid, state in states.items():
            try:
                kept = (
                    self._advance_one(self._slots[vmid], state, repositories, now)
                    if self._protected(vmid, state, repositories)
                    else None
                )
            except IsolationError as err:
                self.blocked = str(err)
                log.error("%s", err)  # noqa: TRY400 - the reason is the message; a traceback adds nothing
                continue
            except PveCiError as err:
                log.warning("VM %s: %s", vmid, err)
                kept = state
            if kept is not None:
                result[vmid] = kept
        return result

    def _advance_one(
        self, slot: Slot, state: VmState, repositories: dict[str, str], now: float
    ) -> VmState | None:
        vmid = slot.vmid
        if state.phase == "booting":
            return self._advance_booting(slot, state, now)
        # Draining gives idle VMs' memory back, so maintenance such as a template build gets room.
        if state.phase == "warm" and (
            state.payload != payload_version() or draining(self._config.daemon.state_dir)
        ):
            self._lifecycle.retire(vmid, state, repositories)
            return None
        if state.phase == "assigned":
            return self._advance_assigned(vmid, state, repositories, now)
        return state

    def _protected(self, vmid: int, state: VmState, repositories: dict[str, str]) -> bool:
        """Whether a running VM is the kernel's first out-of-memory victim; one that cannot be is removed.

        Enforced every cycle: a VM restarted outside the controller, for example by a backup job in stop
        mode or after a host reboot, runs in a new QEMU process with the default priority.
        """
        if not state.running:
            return True
        try:
            self._pve.protect(vmid)
        except PveCiError as err:
            log.warning("VM %s is removed: %s", vmid, err)
            _guarded(vmid, partial(self._lifecycle.retire, vmid, state, repositories))
            return False
        return True

    def _advance_assigned(
        self, vmid: int, state: VmState, repositories: dict[str, str], now: float
    ) -> VmState | None:
        # Checked before any guest call, so a guest that hangs, fails or lies cannot keep its VM alive.
        if now - state.since > self._config.daemon.max_job_seconds:
            log.warning("VM %s outlived the longest allowed job and is removed", vmid)
            self._lifecycle.retire(vmid, state, repositories)
            return None
        if not state.running or self._guest_finished(vmid):
            return None if self._lifecycle.finish(vmid, state, repositories) else state
        if self._lifecycle.outlived_job(vmid, state):
            log.warning("VM %s: GitHub recorded the end of its job but the guest did not; removed", vmid)
            self._lifecycle.retire(vmid, state, repositories)
            return None
        return state

    def _guest_finished(self, vmid: int) -> bool:
        try:
            return self._lifecycle.finished(vmid)
        except PveCiError as err:
            log.warning("VM %s: %s", vmid, err)
            return False

    def _advance_booting(self, slot: Slot, state: VmState, now: float) -> VmState | None:
        if now - state.since > self._config.daemon.boot_timeout_seconds:
            self._pve.destroy(slot.vmid)
            log.warning("VM %s did not boot in time and was removed", slot.vmid)
            return None
        return (self._lifecycle.warm_up(slot, state) if state.running else None) or state

    def _demand(self, repositories: dict[str, str], now: float) -> _Demand:
        demand = _Demand()
        for repository in repositories:
            try:
                jobs = self._github.queued_jobs(repository)
            except PveCiError as err:
                log.warning("%s: queued jobs unknown: %s", repository, err)
                continue
            self._count(demand, repository, jobs, now)
        return demand

    def _count(self, demand: _Demand, repository: str, jobs: list[QueuedJob], now: float) -> None:
        for job in jobs:
            profile = profile_for(job.labels, self._config.profiles)
            if profile is None:
                continue
            demand.queued.setdefault(profile.name, collections.Counter())[repository] += 1
            oldest = demand.oldest.setdefault(profile.name, {})
            oldest[repository] = min(oldest.get(repository, now), job.created_at)

    def _busy(self, repositories: set[str]) -> frozenset[str]:
        busy: set[str] = set()
        for repository in repositories:
            try:
                busy |= self._github.busy_runners(repository)
            except PveCiError as err:
                log.warning("%s: busy runners unknown: %s", repository, err)
        return frozenset(busy)

    def _plan(
        self,
        states: dict[int, VmState],
        infos: dict[int, VmInfo],
        demand: _Demand,
        *,
        busy: frozenset[str],
        now: float,
    ) -> dict[str, Plan]:
        held = draining(self._config.daemon.state_dir) or self.blocked is not None
        free = [v for v in self._slots if v not in infos]
        host = self._pve.host_memory() if free and not held else None
        budget_left = self._config.capacity.memory_mb - self._runner_memory(infos)
        inputs = []
        for profile in self._config.profiles:
            vms = {v: s for v, s in states.items() if s.profile == profile.name}
            queued = {} if held else dict(demand.queued.get(profile.name, {}))
            can_start = 0 if host is None else self._fits(host, budget_left, profile)
            limits = ProfileLimits(
                warm=0 if held else profile.warm, maximum=profile.maximum, can_start=can_start
            )
            inputs.append((profile.name, vms, Demand(queued, demand.oldest.get(profile.name, {})), limits))
        return plan_all(inputs, free, current=Round(busy, now, self._config.daemon.idle_timeout_seconds))

    def _runner_memory(self, infos: dict[int, VmInfo]) -> int:
        return sum(i.memory_mb for v, i in infos.items() if v in self._slots)

    def _fits(self, host: HostMemory, budget_left: int, profile: Profile) -> int:
        return vms_that_fit(
            host,
            reserve_mb=self._config.capacity.host_reserve_mb + reserved_mb(self._config.daemon.state_dir),
            budget_left_mb=budget_left,
            vm_mb=profile.memory_mb,
        )

    def _apply(self, plans: dict[str, Plan], states: dict[int, VmState], infos: dict[int, VmInfo]) -> None:
        for plan in plans.values():
            for vmid in plan.retire:
                _guarded(vmid, partial(self._lifecycle.retire, vmid, states[vmid], self._repositories))
            for vmid, repository in plan.assign:
                _guarded(vmid, partial(self._lifecycle.assign, vmid, states[vmid], repository))
        creations = [(name, vmid) for name, plan in plans.items() for vmid in plan.create]
        if not creations:
            return
        template = current_template(self._config.template.vmids, infos, self._description)
        if template is None:
            log.warning(
                "no finished template among %s; no VM can be created",
                self._config.template.vmids,
            )
            return
        self._create(creations, template[0], infos)

    def _create(self, creations: list[tuple[str, int]], template: int, infos: dict[int, VmInfo]) -> None:
        with memory_lock(self._config.daemon.state_dir):
            host = self._pve.host_memory()
            budget_left = self._config.capacity.memory_mb - self._runner_memory(infos)
            committed = host.committed_mb
            for name, vmid in creations:
                profile = self._config.profile(name)
                if self._fits(HostMemory(host.total_mb, committed), budget_left, profile) < 1:
                    log.info("not starting VM %s: the host keeps its reserve for other guests", vmid)
                    return
                if _guarded(vmid, partial(self._lifecycle.create, self._slots[vmid], profile, template)):
                    budget_left -= profile.memory_mb
                    committed += profile.memory_mb

    def _update_overflow(self, plans: dict[str, Plan], demand: _Demand, now: float) -> None:
        if self._overflow is None:
            return
        name = self._config.overflow.profile if self._config.overflow else ""
        waiting = demand.oldest.get(name, {}).values()
        oldest_wait = now - min(waiting) if waiting else 0.0
        forced = draining(self._config.daemon.state_dir) or self.blocked is not None
        for line in self._overflow.update(
            unserved=plans[name].unserved if name in plans else 0,
            oldest_wait=oldest_wait,
            now=now,
            forced=forced,
        ):
            log.info("%s", line)

    def _maintain(self, states: dict[int, VmState]) -> None:
        in_use = {s.cache.clone for s in states.values() if s.cache is not None}
        try:
            for line in self._cache.maintain(in_use):
                log.info("%s", line)
        except PveCiError as err:
            log.warning("cache maintenance failed: %s", err)
        prune_logs(self._config.daemon.log_dir, self._config.daemon.log_retention_days, self._clock())

    def _description(self, vmid: int) -> str:
        return str(self._pve.config(vmid).get("description", ""))


def _guarded(vmid: int, action: Callable[[], object]) -> bool:
    """Runs one VM action; a failure is logged and leaves the other VMs unaffected."""
    try:
        action()
    except PveCiError as err:
        log.warning("VM %s: %s", vmid, err)
        return False
    return True
