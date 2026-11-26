import dataclasses

import pytest

from pve_ci_runners.config import parse
from pve_ci_runners.errors import GitHubError
from pve_ci_runners.overflow import REASSERT_SECONDS, Hysteresis, Overflow, decide, trigger
from tests.conftest import config_data
from tests.fakes import FakeGitHub


@pytest.fixture
def overflow_config():
    data = config_data()
    data["overflow"] |= {"on_backlog": 2, "on_wait_seconds": 120, "off_seconds": 300}
    config = parse(data).overflow
    assert config is not None
    # The base profile holds two VMs; the decision itself is pure, so wider thresholds need no more slots.
    return dataclasses.replace(config, on_free_slots=1, off_free_slots=3)


def test_the_decision_switches_on_fast_and_off_after_a_calm_period(overflow_config) -> None:
    def step(state: Hysteresis, unserved: int, oldest_wait: float, now: float) -> Hysteresis:
        return decide(
            state, unserved=unserved, oldest_wait=oldest_wait, free=5, now=now, config=overflow_config
        )

    off, on = Hysteresis(), Hysteresis(on=True)
    assert step(off, 2, 0, 1000) == on
    assert step(off, 1, 150, 1000) == on
    assert step(off, 1, 30, 1000) == off
    assert step(on, 0, 0, 1000) == Hysteresis(True, 1000)
    assert step(Hysteresis(True, 1000), 0, 0, 1200).on
    assert not step(Hysteresis(True, 1000), 0, 0, 1301).on
    assert step(Hysteresis(True, 1000), 1, 10, 1301) == on


class Writer:
    def __init__(self, fail: bool = False) -> None:
        self.writes: list[tuple[str, str, str]] = []
        self.fail = fail

    def set_variable(self, repository: str, name: str, value: str) -> None:
        if self.fail:
            raise GitHubError("forbidden", 403)
        self.writes.append((repository, name, value))


def test_the_variable_is_written_once_per_change(overflow_config) -> None:
    writer = Writer()
    overflow = Overflow(overflow_config, writer)

    overflow.update(unserved=0, oldest_wait=0, free=5, now=1000)
    overflow.update(unserved=0, oldest_wait=0, free=5, now=1015)
    lines = overflow.update(unserved=5, oldest_wait=0, free=5, now=1030)

    assert writer.writes == [
        ("example-org/app", "CI_RUNS_ON", '["self-hosted","pve-ci"]'),
        ("example-org/app", "CI_RUNS_ON", '"ubuntu-latest"'),
    ]
    assert overflow.on
    assert lines[0] == "overflow on: 5 jobs without a VM"


def test_forced_overflow_and_failed_writes_are_reported(overflow_config) -> None:
    overflow = Overflow(overflow_config, Writer(fail=True))

    lines = overflow.update(unserved=0, oldest_wait=0, free=5, now=1000, forced=True)

    assert overflow.on
    assert lines[0] == "overflow on: no VM can start"
    assert any("not updated" in line for line in lines)


def test_an_unchanged_value_is_written_again_only_after_the_reassert_interval(overflow_config) -> None:
    writer = Writer()
    overflow = Overflow(overflow_config, writer)

    overflow.update(unserved=0, oldest_wait=0, free=5, now=1000)
    assert overflow.update(unserved=0, oldest_wait=0, free=5, now=1000 + REASSERT_SECONDS - 1) == []
    lines = overflow.update(unserved=0, oldest_wait=0, free=5, now=1000 + REASSERT_SECONDS)

    assert len(writer.writes) == 2 and writer.writes[0] == writer.writes[1]
    assert lines == ['example-org/app: CI_RUNS_ON = ["self-hosted","pve-ci"]']


def test_a_value_overwritten_elsewhere_is_corrected_within_one_interval(overflow_config) -> None:
    github = FakeGitHub()
    overflow = Overflow(overflow_config, github)
    key = ("example-org/app", "CI_RUNS_ON")
    overflow.update(unserved=5, oldest_wait=0, free=5, now=1000)
    github.variables[key] = '["self-hosted","pve-ci"]'

    overflow.update(unserved=5, oldest_wait=0, free=5, now=1000 + REASSERT_SECONDS)

    assert github.variables[key] == '"ubuntu-latest"'


def test_low_free_capacity_switches_on_before_any_job_waits(overflow_config) -> None:
    off = Hysteresis()
    assert decide(off, unserved=0, oldest_wait=0, free=1, now=1000, config=overflow_config).on
    assert decide(off, unserved=0, oldest_wait=0, free=0, now=1000, config=overflow_config).on
    assert decide(off, unserved=0, oldest_wait=0, free=2, now=1000, config=overflow_config) == off


def test_overflow_stays_on_through_a_burst(overflow_config) -> None:
    state = Hysteresis(on=True)
    for now, unserved, free in [(1000, 0, 0), (1015, 4, 0), (1030, 1, 3), (1045, 0, 2), (1060, 0, 1)]:
        state = decide(state, unserved=unserved, oldest_wait=0, free=free, now=now, config=overflow_config)
        assert state == Hysteresis(on=True), f"still on at {now}"


def test_overflow_switches_off_only_after_off_seconds_of_enough_free_capacity(overflow_config) -> None:
    def step(state: Hysteresis, now: float, free: int) -> Hysteresis:
        return decide(state, unserved=0, oldest_wait=0, free=free, now=now, config=overflow_config)

    state = step(Hysteresis(on=True), 1000, 3)
    assert state == Hysteresis(True, 1000)
    state = step(state, 1100, 2)
    assert state == Hysteresis(on=True), "below off_free_slots the calm period starts over"
    state = step(step(state, 1150, 3), 1449, 4)
    assert state == Hysteresis(True, 1150)
    assert step(state, 1450, 3) == Hysteresis(False, 1150)


def test_capacity_hovering_around_the_threshold_does_not_flap(overflow_config) -> None:
    writer = Writer()
    overflow = Overflow(overflow_config, writer)
    lines: list[str] = []
    for tick in range(40):
        lines += overflow.update(unserved=0, oldest_wait=0, free=2 - tick % 2, now=1000 + 15 * tick)
    for tick in range(40, 60):
        lines += overflow.update(unserved=0, oldest_wait=0, free=2 + tick % 2, now=1000 + 15 * tick)

    assert [line for line in lines if line.startswith("overflow")] == ["overflow on: 1 slot free"]
    values = [value for _, _, value in writer.writes]
    assert values[0] == '["self-hosted","pve-ci"]' and set(values[1:]) == {'"ubuntu-latest"'}

    for tick in range(60, 81):
        lines += overflow.update(unserved=0, oldest_wait=0, free=3, now=1000 + 15 * tick)
    assert lines[-2:] == [
        "overflow off: 3 slots free and nothing unserved for 300 s",
        'example-org/app: CI_RUNS_ON = ["self-hosted","pve-ci"]',
    ]


@pytest.mark.parametrize(
    ("unserved", "oldest_wait", "free", "reason"),
    [(2, 0, 5, "2 jobs without a VM"), (1, 150, 5, "a job waits for 150 s"), (0, 0, 0, "0 slots free")],
)
def test_the_switch_names_its_reason(overflow_config, unserved, oldest_wait, free, reason) -> None:
    overflow = Overflow(overflow_config, Writer())

    lines = overflow.update(unserved=unserved, oldest_wait=oldest_wait, free=free, now=1000)

    assert lines[0] == f"overflow on: {reason}"


@pytest.mark.parametrize("unserved", [0, 1, 50])
@pytest.mark.parametrize("free", [0, 1, 5])
def test_disabled_backlog_and_free_slot_triggers_never_fire_but_the_wait_does(
    overflow_config, unserved: int, free: int
) -> None:
    config = dataclasses.replace(overflow_config, on_backlog=0, on_free_slots=-1)

    assert trigger(unserved=unserved, oldest_wait=119, free=free, config=config) is None
    assert trigger(unserved=unserved, oldest_wait=120, free=free, config=config) == "a job waits for 120 s"


def test_each_trigger_turns_off_on_its_own(overflow_config) -> None:
    no_backlog = dataclasses.replace(overflow_config, on_backlog=0)
    no_free = dataclasses.replace(overflow_config, on_free_slots=-1)

    assert trigger(unserved=9, oldest_wait=0, free=0, config=no_backlog) == "0 slots free"
    assert trigger(unserved=9, oldest_wait=0, free=0, config=no_free) == "9 jobs without a VM"


def test_zero_and_minus_one_are_valid_and_keep_the_hysteresis_rule() -> None:
    data = config_data()
    data["overflow"] |= {"on_backlog": 0, "on_free_slots": -1, "off_free_slots": 1}
    config = parse(data).overflow

    assert config is not None and (config.on_backlog, config.on_free_slots) == (0, -1)
