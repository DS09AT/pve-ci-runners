import random

from pve_ci_runners.scheduler import Demand, Plan, ProfileLimits, Round, plan_all, plan_profile
from pve_ci_runners.state import VmState

PAYLOAD = "0123456789abcdef"
WARM = VmState("warm", 0, "default", PAYLOAD)
BOOTING = VmState("booting", 0, "default", PAYLOAD)
LIMITS = ProfileLimits(warm=2, maximum=2, can_start=2)


def assigned(runner: str, repository: str, since: int = 990, started: int | None = None) -> VmState:
    return VmState(
        "assigned", since, "default", PAYLOAD, repository=repository, runner=runner, job_started=started
    )


def plan(vms, free, queued=None, waiting=None, busy=frozenset(), limits=LIMITS, now=1000.0) -> Plan:
    demand = Demand(queued or {}, waiting or {})
    return plan_profile(vms, free, demand, limits, current=Round(frozenset(busy), now, 600))


def test_an_idle_host_fills_its_warm_pool() -> None:
    assert plan({}, [121, 122]) == Plan(create=(121, 122))


def test_a_warm_vm_takes_a_queued_job_and_a_new_one_boots() -> None:
    result = plan({121: WARM}, [122], {"o/r": 3})

    assert result.assign == ((121, "o/r"),)
    assert result.create == (122,)


def test_an_assigned_runner_that_has_not_started_covers_one_job() -> None:
    result = plan({121: assigned("x", "o/r"), 122: WARM}, [], {"o/r": 1}, busy={"x"})

    assert result.assign == ((122, "o/r"),)


def test_an_idle_runner_is_retired_after_the_timeout_unless_its_job_started() -> None:
    assert plan({121: assigned("x", "o/r", since=0), 122: WARM}, []).retire == (121,)
    assert plan({121: assigned("x", "o/r", since=0, started=5), 122: WARM}, []).retire == ()


def test_the_repository_waiting_longest_gets_the_next_vm() -> None:
    result = plan(
        {121: WARM, 122: assigned("y", "o/q")},
        [],
        {"o/Abc": 1, "o/xyz": 1},
        {"o/Abc": 900, "o/xyz": 100},
        {"y"},
    )

    assert result.assign == ((121, "o/xyz"),)


def test_a_repository_holding_vms_does_not_get_the_next_one_while_another_waits() -> None:
    result = plan(
        {121: assigned("a", "o/Big"), 122: WARM},
        [],
        {"o/Big": 5, "o/small": 1},
        {"o/Big": 100, "o/small": 900},
        {"a"},
    )

    assert result.assign == ((122, "o/small"),)


def test_the_warm_pool_is_refilled_after_assignments() -> None:
    limits = ProfileLimits(warm=1, maximum=3, can_start=3)

    result = plan({121: WARM}, [122, 123], {"o/r": 1}, limits=limits)

    assert result.assign == ((121, "o/r"),)
    assert result.create == (122,)


def test_creation_stops_at_the_profile_maximum_and_the_memory_guard() -> None:
    assert plan({}, [121, 122], {"o/r": 5}, limits=ProfileLimits(2, 1, 5)).create == (121,)
    assert plan({}, [121, 122], {"o/r": 5}, limits=ProfileLimits(2, 2, 0)).create == ()


def test_unserved_counts_jobs_no_vm_is_coming_for() -> None:
    assert plan({}, [121], {"o/r": 4}, limits=ProfileLimits(0, 1, 1)).unserved == 3
    assert plan({121: BOOTING}, [], {"o/r": 1}).unserved == 0


def test_profiles_take_free_slots_in_configuration_order() -> None:
    current = Round(frozenset(), 1000.0, 600)
    first = ("small", {}, Demand({"o/r": 1}), ProfileLimits(0, 1, 1))
    second = ("large", {}, Demand({"o/r": 1}), ProfileLimits(0, 1, 1))

    plans = plan_all([first, second], [122, 121], current=current)

    assert plans["small"].create == (121,)
    assert plans["large"].create == (122,)


def _random_vms(rng: random.Random, slots: list[int]) -> dict[int, VmState]:
    vms: dict[int, VmState] = {}
    for vmid in rng.sample(slots, rng.randint(0, len(slots))):
        phase = rng.choice(["warm", "booting", "assigned"])
        if phase == "assigned":
            started = rng.choice([None, 950])
            vms[vmid] = assigned(
                f"r{vmid}", rng.choice(["o/a", "o/b"]), since=rng.choice([0, 990]), started=started
            )
        else:
            vms[vmid] = VmState(phase, 0, "default", PAYLOAD)
    return vms


def test_invariants_over_random_states() -> None:
    rng = random.Random(20261121)
    slots = list(range(100, 108))
    for _ in range(2000):
        vms = _random_vms(rng, slots)
        free = [s for s in slots if s not in vms]
        queued = {r: n for r in ("o/a", "o/b") if (n := rng.randint(0, 4))}
        busy = frozenset(s.runner or "" for s in vms.values() if s.phase == "assigned" and rng.random() < 0.5)
        limits = ProfileLimits(warm=rng.randint(0, 3), maximum=rng.randint(1, 8), can_start=rng.randint(0, 8))

        result = plan(vms, free, queued, busy=busy, limits=limits)

        assigned_to = [v for v, _ in result.assign]
        assert len(set(assigned_to)) == len(assigned_to), "a VM is assigned twice"
        assert all(vms[v].phase == "warm" for v in assigned_to), "only warm VMs are assigned"
        assert set(result.create) <= set(free), "only free slots are used"
        assert len(result.create) <= limits.can_start, "the memory guard is respected"
        assert len(vms) - len(result.retire) + len(result.create) <= max(limits.maximum, len(vms)), (
            "never above max"
        )
        for vmid in result.retire:
            assert vms[vmid].phase == "assigned" and vms[vmid].job_started is None, (
                "a running job is never retired"
            )
            assert vms[vmid].runner not in busy
        assert len(result.assign) <= sum(queued.values()), "no job is served twice"
        reordered = dict(reversed(list(vms.items())))
        assert plan(reordered, list(reversed(free)), queued, busy=busy, limits=limits) == result, (
            "order does not matter"
        )


def test_a_stuck_assignment_is_retired_although_jobs_wait_and_the_job_is_served_again() -> None:
    stuck = VmState(phase="assigned", since=0, profile="p", payload="0" * 16, repository="o/r", runner="r-1")
    warm = VmState(phase="warm", since=0, profile="p", payload="0" * 16)
    current = Round(frozenset(), 1000.0, 600, assign_timeout=900)

    result = plan_profile(
        {1: stuck, 2: warm},
        [],
        Demand({"o/r": 1}),
        ProfileLimits(warm=0, maximum=2, can_start=0),
        current=current,
    )

    assert result.retire == (1,) and result.stuck == (1,)
    assert result.assign == ((2, "o/r"),)
    early = plan_profile(
        {1: stuck},
        [],
        Demand({"o/r": 1}),
        ProfileLimits(warm=0, maximum=2, can_start=0),
        current=Round(frozenset(), 800.0, 600, assign_timeout=900),
    )
    assert early.retire == () and early.stuck == ()
