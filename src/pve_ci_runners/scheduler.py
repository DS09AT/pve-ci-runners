"""Which VMs to create, hand to a repository or retire: pure functions over plain data.

Each profile owns a fixed share of the slots (`max`), so profiles never compete for a VM and the plan
for one profile does not depend on another, except for the free slots and memory both draw from.
"""

import collections
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pve_ci_runners.state import VmState


@dataclass(frozen=True)
class Demand:
    """Queued jobs of one profile per repository, and since when the oldest of them waits."""

    queued: Mapping[str, int]
    waiting_since: Mapping[str, float] = field(default_factory=dict[str, float])


@dataclass(frozen=True)
class ProfileLimits:
    """What one profile may hold and keep ready."""

    warm: int
    maximum: int
    can_start: int


@dataclass(frozen=True)
class Plan:
    """The actions one planning round decided on."""

    assign: tuple[tuple[int, str], ...] = ()
    create: tuple[int, ...] = ()
    retire: tuple[int, ...] = ()
    unserved: int = 0


@dataclass(frozen=True)
class Round:
    """What one planning round knows beyond the VMs: busy runners and the time."""

    busy: frozenset[str]
    now: float
    idle_timeout: float


def plan_profile(
    vms: Mapping[int, VmState],
    free_slots: Sequence[int],
    demand: Demand,
    limits: ProfileLimits,
    *,
    current: Round,
) -> Plan:
    """Plans one profile.

    An assigned runner that has not started a job yet covers one queued job of its repository, so a job
    is never served twice. A runner that has started a job is never retired, whatever the API reports
    in between. Each warm VM goes to the repository with the fewest VMs, then the oldest waiting job.
    """
    assigned = {v: s for v, s in vms.items() if s.phase == "assigned"}
    idle = {v: s for v, s in assigned.items() if s.runner not in current.busy and s.job_started is None}
    retire = _retirements(idle, demand, current)
    warm = sorted(v for v, s in vms.items() if s.phase == "warm")
    booting = sum(1 for s in vms.values() if s.phase == "booting")
    assign, unmet = _assign(
        list(warm),
        demand,
        covered=collections.Counter(s.repository for v, s in idle.items() if v not in retire),
        active=collections.Counter(s.repository for v, s in assigned.items() if v not in retire),
        now=current.now,
    )
    # The warm pool is refilled from what assignments leave over, not from what was warm before.
    still_warm = len(warm) - len(assign)
    wanted = max(0, unmet - booting) + max(0, limits.warm - still_warm - max(0, booting - unmet))
    room = max(0, limits.maximum - (len(vms) - len(retire)))
    count = min(wanted, room, len(free_slots), limits.can_start)
    return Plan(
        assign=tuple(assign),
        create=tuple(sorted(free_slots)[:count]),
        retire=retire,
        unserved=max(0, unmet - booting - count),
    )


def _retirements(idle: Mapping[int, VmState], demand: Demand, current: Round) -> tuple[int, ...]:
    return tuple(
        sorted(
            v
            for v, s in idle.items()
            if current.now - s.since > current.idle_timeout and demand.queued.get(s.repository or "", 0) == 0
        )
    )


def plan_all(
    profiles: Sequence[tuple[str, Mapping[int, VmState], Demand, ProfileLimits]],
    free_slots: Sequence[int],
    *,
    current: Round,
) -> dict[str, Plan]:
    """Plans every profile in configuration order; earlier profiles take free slots first."""
    remaining = sorted(free_slots)
    plans: dict[str, Plan] = {}
    for name, vms, demand, limits in profiles:
        result = plan_profile(vms, remaining, demand, limits, current=current)
        remaining = [s for s in remaining if s not in result.create]
        plans[name] = result
    return plans


def _assign(
    warm: list[int],
    demand: Demand,
    *,
    covered: collections.Counter[str | None],
    active: collections.Counter[str | None],
    now: float,
) -> tuple[list[tuple[int, str]], int]:
    pending = {r: n - covered[r] for r, n in demand.queued.items() if n > covered[r]}
    assign: list[tuple[int, str]] = []
    unmet = 0
    while pending:
        repository = min(pending, key=lambda r: (active[r], demand.waiting_since.get(r, now), r))
        if warm:
            assign.append((warm.pop(0), repository))
            active[repository] += 1
        else:
            unmet += 1
        pending[repository] -= 1
        if pending[repository] == 0:
            del pending[repository]
    return assign, unmet
