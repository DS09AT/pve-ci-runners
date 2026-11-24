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
from pve_ci_runners.ports import DiskSpace, GitHubActions, Hypervisor
from pve_ci_runners.pve import TAG, VmInfo
from pve_ci_runners.scheduler import Demand, Plan, ProfileLimits, Round, plan_all
from pve_ci_runners.state import VmState, decode
from pve_ci_runners.templates import current_template, reserved_build_vms

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

    def __init__(  # noqa: PLR0913 - one argument per port the controller drives
        self,
        config: Config,
        hypervisor: Hypervisor,
        github: GitHubActions,
        cache: CacheBackend,
        *,
        disk: DiskSpace,
        overflow: Overflow | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._pve = hypervisor
        self._github = github
        self._cache = cache
        self._disk = disk
        self._overflow = overflow
        self._clock = clock
        self._lifecycle = Lifecycle(config, hypervisor, github, cache, clock=clock)
        self._slots = {s.vmid: s for s in config.slots}
        self._repositories: dict[str, str] = {}
        self._repositories_at = -float(_REPOSITORY_REFRESH)
        self._maintained_at = -float(_MAINTENANCE_INTERVAL)
        # Counted in memory only: after a restart a broken guest just fails its checks once more.
        self._failures: dict[int, int] = {}
        self._disk_room = 0
        self.blocked: str | None = None

    def cycle(self) -> None:
        """Finishes what is done, plans, and starts what is needed."""
        now = self._clock()
        repositories = self._served_repositories(now)
        infos = self._pve.vms()
        states = self._advance(self._states(infos), repositories, now)
        states = self._make_disk_room(states, repositories)
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
        # A VM boots before its guest is checked, so a count never passes from one VM to the next in a slot.
        self._failures = {
            v: n for v, n in self._failures.items() if v in states and states[v].phase != "booting"
        }
        result: dict[int, VmState] = {}
        for vmid, state in states.items():
            kept = self._advance_guarded(vmid, state, repositories, now)
            if kept is not None:
                result[vmid] = kept
        return result

    def _advance_guarded(
        self, vmid: int, state: VmState, repositories: dict[str, str], now: float
    ) -> VmState | None:
        try:
            if not self._protected(vmid, state, repositories):
                return None
            return self._advance_one(self._slots[vmid], state, repositories, now)
        except IsolationError as err:
            self.blocked = str(err)
            log.error("%s", err)  # noqa: TRY400 - the reason is the message; a traceback adds nothing
            return None
        except PveCiError as err:
            log.warning("VM %s: %s", vmid, err)
            return state

    def _advance_one(
        self, slot: Slot, state: VmState, repositories: dict[str, str], now: float
    ) -> VmState | None:
        vmid = slot.vmid
        if state.phase == "booting":
            return self._advance_booting(slot, state, now)
        if state.phase == "warm":
            return self._advance_warm(vmid, state, repositories)
        if state.phase == "assigned":
            return self._advance_assigned(vmid, state, repositories, now)
        return state

    def _advance_warm(self, vmid: int, state: VmState, repositories: dict[str, str]) -> VmState | None:
        # Draining gives idle VMs' memory back, so maintenance such as a template build gets room.
        if state.payload != payload_version() or draining(self._config.daemon.state_dir):
            self._lifecycle.retire(vmid, state, repositories)
            return None
        if state.running:
            self._guest_finished(vmid, state)
        return None if self._retired_as_broken(vmid, state, repositories) else state

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
        if not state.running or self._guest_finished(vmid, state):
            return None if self._lifecycle.finish(vmid, state, repositories) else state
        if self._retired_as_broken(vmid, state, repositories) or self._retired_as_ended(
            vmid, state, repositories
        ):
            return None
        return state

    def _retired_as_ended(self, vmid: int, state: VmState, repositories: dict[str, str]) -> bool:
        if not self._lifecycle.outlived_job(vmid, state):
            return False
        log.warning("VM %s: GitHub recorded the end of its job but the guest did not; removed", vmid)
        self._lifecycle.retire(vmid, state, repositories)
        return True

    def _guest_finished(self, vmid: int, state: VmState) -> bool:
        """Whether the runner exited; a failed check counts against the guest's health."""
        try:
            finished = self._lifecycle.poll_finished(vmid, state)
        except PveCiError as err:
            self._failures[vmid] = self._failures.get(vmid, 0) + 1
            log.warning("VM %s: guest check failed (%d in a row): %s", vmid, self._failures[vmid], err)
            return False
        if finished is not None:
            self._failures.pop(vmid, None)
        return finished is True

    def _retired_as_broken(self, vmid: int, state: VmState, repositories: dict[str, str]) -> bool:
        """Retires a VM whose guest failed too many checks in a row, its cache discarded.

        An unusable guest, for example one whose disk returns I/O errors, never reports the end of its job,
        so waiting for that would hold the slot until `daemon.max_job_seconds`.
        """
        failures = self._failures.get(vmid, 0)
        if failures < self._config.daemon.unhealthy_checks:
            return False
        log.warning("VM %s is retired: its guest failed %d checks in a row", vmid, failures)
        del self._failures[vmid]
        self._lifecycle.retire(vmid, state, repositories)
        return True

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

    def _make_disk_room(self, states: dict[int, VmState], repositories: dict[str, str]) -> dict[int, VmState]:
        """Sets how many more VMs the disk takes, after freeing caches and then idle VMs if it is short.

        Every VM, running or about to start, keeps `capacity.disk_reserve_gb` free for what its job will
        still write. Caches are evicted as soon as the room is below the free slots, so a start waits for
        no hourly maintenance; idle VMs go only when even the present VMs lack their reserve. Below that
        no VM starts or takes a job, so the jobs count as unserved; the room is measured again every
        cycle, so the controller resumes on its own once space is back.
        """
        room = self._disk_fit(states)
        if room is not None and room < len(self._slots) - len(states):
            self._maintain(states)
            room = self._disk_fit(states)
        kept = dict(states)
        idle = sorted(v for v, s in states.items() if s.phase != "assigned")
        while room is not None and room < 0 and idle:
            vmid = idle.pop(0)
            log.warning("VM %s is retired: the disk lacks the write reserve of the VMs running jobs", vmid)
            if _guarded(vmid, partial(self._lifecycle.retire, vmid, kept[vmid], repositories)):
                del kept[vmid]
                room += 1
        self._disk_room = -1 if room is None else room
        return kept

    def _disk_fit(self, states: dict[int, VmState]) -> int | None:
        """Additional VMs the disk holds a reserve for; negative when the present ones lack theirs."""
        try:
            available = self._disk.available()
        except PveCiError as err:
            log.warning("free disk space unknown, no VM starts or takes a job: %s", err)
            return None
        if available is None:
            return len(self._slots)
        # A template build holding its memory reservation runs, or is about to, and keeps a reserve too.
        building = 1 if reserved_mb(self._config.daemon.state_dir) else 0
        return available // self._config.capacity.disk_reserve_bytes - len(states) - building

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
        # Without a template nothing can start, so the jobs count as unserved and overflow can take them.
        startable = bool(free) and not held and self._template(infos) is not None
        host = self._pve.host_memory(reserved_build_vms(self._config)) if startable else None
        budget_left = self._config.capacity.memory_mb - self._runner_memory(infos)
        inputs = []
        for profile in self._config.profiles:
            # Without disk room an idle VM cannot take a job, so it must not count as serving one.
            vms = {
                v: s
                for v, s in states.items()
                if s.profile == profile.name and (self._disk_room >= 0 or s.phase == "assigned")
            }
            queued = {} if held else dict(demand.queued.get(profile.name, {}))
            can_start = (
                0 if host is None else min(self._fits(host, budget_left, profile), max(0, self._disk_room))
            )
            limits = ProfileLimits(
                warm=0 if held else profile.warm, maximum=profile.maximum, can_start=can_start
            )
            inputs.append((profile.name, vms, Demand(queued, demand.oldest.get(profile.name, {})), limits))
        daemon = self._config.daemon
        current = Round(busy, now, daemon.idle_timeout_seconds, daemon.assign_timeout_seconds)
        return plan_all(inputs, free, current=current)

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
            self._retire_and_assign(plan, states)
        creations = [(name, vmid) for name, plan in plans.items() for vmid in plan.create]
        template = self._template(infos) if creations else None
        if template is not None:
            self._create(creations, template, infos)

    def _retire_and_assign(self, plan: Plan, states: dict[int, VmState]) -> None:
        for vmid in plan.retire:
            if vmid in plan.stuck:
                log.warning("VM %s is retired: its runner took no job within the assign timeout", vmid)
            _guarded(vmid, partial(self._lifecycle.retire, vmid, states[vmid], self._repositories))
        for vmid, repository in plan.assign:
            _guarded(vmid, partial(self._lifecycle.assign, vmid, states[vmid], repository))

    def _template(self, infos: dict[int, VmInfo]) -> int | None:
        template = current_template(self._config.template.vmids, infos, self._description)
        if template is None:
            log.warning("no finished template among %s; no VM can be created", self._config.template.vmids)
            return None
        return template[0]

    def _create(self, creations: list[tuple[str, int]], template: int, infos: dict[int, VmInfo]) -> None:
        with memory_lock(self._config.daemon.state_dir):
            host = self._pve.host_memory(reserved_build_vms(self._config))
            budget_left = self._config.capacity.memory_mb - self._runner_memory(infos)
            committed = host.committed_mb
            for name, vmid in creations:
                profile = self._config.profile(name)
                if self._fits(HostMemory(host.total_mb, committed), budget_left, profile) < 1:
                    log.info("not starting VM %s: the host keeps its reserve for other guests", vmid)
                    return
                if self._disk_room < 1:
                    log.warning("not starting VM %s: the disk keeps the write reserve of every VM", vmid)
                    return
                if _guarded(vmid, partial(self._lifecycle.create, self._slots[vmid], profile, template)):
                    budget_left -= profile.memory_mb
                    committed += profile.memory_mb
                    self._disk_room -= 1

    def _update_overflow(self, plans: dict[str, Plan], demand: _Demand, now: float) -> None:
        if self._overflow is None:
            return
        name = self._config.overflow.profile if self._config.overflow else ""
        waiting = demand.oldest.get(name, {}).values()
        oldest_wait = now - min(waiting) if waiting else 0.0
        plan = plans.get(name, Plan())
        unserved = plan.unserved
        # Without disk room no start can succeed, so waiting for one would only delay the jobs.
        forced = (
            draining(self._config.daemon.state_dir)
            or self.blocked is not None
            or (self._disk_room < 1 and unserved > 0)
        )
        for line in self._overflow.update(
            unserved=unserved,
            oldest_wait=oldest_wait,
            free=plan.spare,
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
