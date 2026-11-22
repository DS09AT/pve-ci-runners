import pytest

from pve_ci_runners.config import parse
from pve_ci_runners.errors import GitHubError
from pve_ci_runners.overflow import REASSERT_SECONDS, Hysteresis, Overflow, decide
from tests.conftest import config_data
from tests.fakes import FakeGitHub


@pytest.fixture
def overflow_config():
    data = config_data()
    data["overflow"] |= {"on_backlog": 2, "on_wait_seconds": 120, "off_seconds": 300}
    config = parse(data).overflow
    assert config is not None
    return config


def test_the_decision_switches_on_fast_and_off_after_a_calm_period(overflow_config) -> None:
    off = Hysteresis()
    assert decide(off, unserved=2, oldest_wait=0, now=1000, config=overflow_config) == Hysteresis(on=True)
    assert decide(off, unserved=1, oldest_wait=150, now=1000, config=overflow_config) == Hysteresis(on=True)
    assert decide(off, unserved=1, oldest_wait=30, now=1000, config=overflow_config) == Hysteresis()
    on = Hysteresis(on=True)
    assert decide(on, unserved=0, oldest_wait=0, now=1000, config=overflow_config) == Hysteresis(True, 1000)
    assert decide(Hysteresis(True, 1000), unserved=0, oldest_wait=0, now=1200, config=overflow_config).on
    assert not decide(Hysteresis(True, 1000), unserved=0, oldest_wait=0, now=1301, config=overflow_config).on
    assert decide(Hysteresis(True, 1000), unserved=1, oldest_wait=10, now=1301, config=overflow_config) == on


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

    overflow.update(unserved=0, oldest_wait=0, now=1000)
    overflow.update(unserved=0, oldest_wait=0, now=1015)
    lines = overflow.update(unserved=5, oldest_wait=0, now=1030)

    assert writer.writes == [
        ("example-org/app", "CI_RUNS_ON", '["self-hosted","pve-ci"]'),
        ("example-org/app", "CI_RUNS_ON", '"ubuntu-latest"'),
    ]
    assert overflow.on
    assert lines[0].startswith("overflow on")


def test_forced_overflow_and_failed_writes_are_reported(overflow_config) -> None:
    overflow = Overflow(overflow_config, Writer(fail=True))

    lines = overflow.update(unserved=0, oldest_wait=0, now=1000, forced=True)

    assert overflow.on
    assert any("not updated" in line for line in lines)


def test_an_unchanged_value_is_written_again_only_after_the_reassert_interval(overflow_config) -> None:
    writer = Writer()
    overflow = Overflow(overflow_config, writer)

    overflow.update(unserved=0, oldest_wait=0, now=1000)
    assert overflow.update(unserved=0, oldest_wait=0, now=1000 + REASSERT_SECONDS - 1) == []
    lines = overflow.update(unserved=0, oldest_wait=0, now=1000 + REASSERT_SECONDS)

    assert len(writer.writes) == 2 and writer.writes[0] == writer.writes[1]
    assert lines == ['example-org/app: CI_RUNS_ON = ["self-hosted","pve-ci"]']


def test_a_value_overwritten_elsewhere_is_corrected_within_one_interval(overflow_config) -> None:
    github = FakeGitHub()
    overflow = Overflow(overflow_config, github)
    key = ("example-org/app", "CI_RUNS_ON")
    overflow.update(unserved=5, oldest_wait=0, now=1000)
    github.variables[key] = '["self-hosted","pve-ci"]'

    overflow.update(unserved=5, oldest_wait=0, now=1000 + REASSERT_SECONDS)

    assert github.variables[key] == '"ubuntu-latest"'
