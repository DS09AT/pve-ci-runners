from pathlib import Path

import pytest

from pve_ci_runners.config import Config, parse
from pve_ci_runners.drain import draining, reserve, reserved_mb
from pve_ci_runners.errors import GitHubError, PveCiError
from pve_ci_runners.pve import TAG, Pve
from pve_ci_runners.state import CacheLease, VmState
from pve_ci_runners.teardown import Teardown
from pve_ci_runners.template_build import template_lock
from pve_ci_runners.templates import TemplateMarker
from tests.conftest import config_data
from tests.fakes import FakeCache, FakeGitHub, FakeHost, FakeVm, Script, done

RUNNER = "ci-runner-121-abc123"
LEASE = CacheLease(
    "o__r/ci__build", "rpool/ci-cache/jobs/o__r.ci__build-121-1", "rpool/ci-cache/repos/o__r/ci__build@g1"
)


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup(tmp_path: Path):
    config = parse(
        config_data(daemon={"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs")})
    )
    host = FakeHost()
    host.snippet_dir = tmp_path
    return config, host, FakeGitHub(), FakeCache(), Clock()


def ours(host: FakeHost, vmid: int, name: str, state: VmState | None = None, *, running: bool = True) -> None:
    description = state.encode() if state else TemplateMarker(100, "2.338.2").encode()
    host.machines[vmid] = FakeVm(name=name, running=running, tags={TAG}, description=description)
    host.firewall[vmid] = "[OPTIONS]\n"


def assigned(since: float = 9_000, **changes: object) -> VmState:
    fields: dict[str, object] = {"repository": "example-org/app", "runner": RUNNER, "cache": LEASE, **changes}
    return VmState("assigned", int(since), "default", "0" * 16, **fields)  # type: ignore[arg-type]  # the fields are of mixed types


def teardown(config: Config, host: FakeHost, github: FakeGitHub, cache: FakeCache, clock: Clock) -> Teardown:
    def sleep(seconds: float) -> None:
        clock.now += seconds

    return Teardown(config, host, github, cache, clock=clock, sleep=sleep)


def test_only_our_vms_go_and_their_registrations_firewall_and_snippets_with_them(setup) -> None:
    config, host, github, cache, clock = setup
    ours(host, 121, "ci-runner-121", assigned())
    host.machines[121].done = True
    ours(host, 9201, "ci-runner-template-9201")
    host.machines[9201].template = True
    host.machines[122] = FakeVm(name="ci-runner-122", tags={"other"})
    host.machines[9202] = FakeVm(name="database", tags={TAG})
    host.machines[300] = FakeVm(name="ci-runner-300", tags={TAG})
    host.firewall.update({122: "foreign", 9202: "foreign"})
    snippet = host.snippet_dir / "pve-ci-runners-build-9201.yaml"
    snippet.write_text("#cloud-config\n", encoding="utf-8")
    reserve(config.daemon.state_dir, 4096)

    assert teardown(config, host, github, cache, clock).run(purge_cache=False) == []

    assert set(host.machines) == {122, 9202, 300}, "foreign VMs on our VMIDs and other VMIDs stay"
    assert host.events == ["destroy 121", "destroy 9201"], "linked clones go before their template"
    assert host.firewall == {122: "foreign", 9202: "foreign"}
    assert github.removed == [RUNNER]
    assert cache.dropped == [LEASE.key] and not cache.written and not cache.purged
    assert not snippet.exists()
    assert draining(config.daemon.state_dir) and reserved_mb(config.daemon.state_dir) == 0


def test_a_second_teardown_finds_nothing_and_succeeds_without_github(setup) -> None:
    config, host, github, cache, clock = setup
    ours(host, 121, "ci-runner-121", assigned())
    host.machines[121].done = True
    run = teardown(config, host, github, cache, clock)
    assert run.run(purge_cache=True) == []
    github.repos = {}

    assert run.run(purge_cache=True) == []
    assert host.events == ["destroy 121"] and github.removed == [RUNNER]
    assert cache.purged


def test_the_cache_is_purged_only_when_asked(setup) -> None:
    config, host, github, cache, clock = setup

    teardown(config, host, github, cache, clock).run(purge_cache=False)
    assert not cache.purged
    teardown(config, host, github, cache, clock).run(purge_cache=True)
    assert cache.purged


def test_running_jobs_are_waited_for(setup) -> None:
    config, host, github, cache, clock = setup
    ours(host, 121, "ci-runner-121", assigned())
    ours(host, 122, "ci-runner-122", assigned(runner="ci-runner-122-def456"), running=False)
    start = clock.now

    def sleep(seconds: float) -> None:
        clock.now += seconds
        host.machines[121].done = clock.now - start >= 60

    Teardown(config, host, github, cache, clock=clock, sleep=sleep).run(purge_cache=False)

    assert clock.now - start == 60, "the wait ends once the guest reports the runner finished"
    assert host.events == ["destroy 121", "destroy 122"]


def test_the_wait_is_bounded_by_the_longest_allowed_job_even_for_a_silent_guest(setup) -> None:
    config, host, github, cache, clock = setup
    ours(host, 121, "ci-runner-121", assigned(since=clock.now))
    host.machines[121].silent = True
    start = clock.now

    teardown(config, host, github, cache, clock).run(purge_cache=False)

    assert config.daemon.max_job_seconds < clock.now - start <= config.daemon.max_job_seconds + 15
    assert 121 not in host.machines


def test_every_failure_is_reported_and_the_other_steps_still_run(setup) -> None:
    config, host, _, cache, clock = setup

    class FailingGitHub(FakeGitHub):
        def remove_runner(self, repository: str, name: str) -> None:
            raise GitHubError("runner removal refused", 500)

    ours(host, 121, "ci-runner-121", assigned())
    ours(host, 122, "ci-runner-122", assigned(runner="ci-runner-122-def456"))
    host.machines[121].done = host.machines[122].done = True
    host.undestroyable.add(121)
    host.snippet_dir /= "not-a-directory"
    host.snippet_dir.write_text("", encoding="utf-8")

    failures = teardown(config, host, FailingGitHub(), cache, clock).run(purge_cache=False)

    assert [f.split(":", 1)[0] for f in failures] == [
        "runner of VM 121",
        "VM 121",
        "runner of VM 122",
        "build snippet 9201",
        "build snippet 9202",
    ]
    assert host.events == ["destroy 122"], "a failed registration does not keep its VM alive"


def test_unknown_served_repositories_fail_the_teardown_but_remove_the_vms(setup) -> None:
    config, host, _, cache, clock = setup

    class Unreachable(FakeGitHub):
        def repositories(self) -> dict[str, str]:
            raise GitHubError("unreachable")

    ours(host, 121, "ci-runner-121", assigned())
    host.machines[121].done = True
    unreachable = Unreachable()

    failures = teardown(config, host, unreachable, cache, clock).run(purge_cache=False)

    assert len(failures) == 1 and "served repositories unknown" in failures[0]
    assert 121 not in host.machines and unreachable.removed == []


def test_a_runner_the_node_did_not_register_is_left_alone(setup) -> None:
    config, host, github, cache, clock = setup
    ours(host, 121, "ci-runner-121", assigned(runner="build-agent-7"))
    ours(host, 122, "ci-runner-122", assigned(runner="ci-runner-122-def456", repository="example-org/other"))
    host.machines[121].done = host.machines[122].done = True
    ours(host, 9201, "ci-runner-template-9201")
    host.machines[9201].description = "not a state"

    assert teardown(config, host, github, cache, clock).run(purge_cache=False) == []
    assert github.removed == []
    assert host.events == ["destroy 121", "destroy 122", "destroy 9201"]


def test_no_teardown_runs_beside_a_template_build(setup) -> None:
    config, host, github, cache, clock = setup
    ours(host, 9201, "ci-runner-template-9201")

    with template_lock(config.daemon.state_dir), pytest.raises(PveCiError, match="template build"):
        teardown(config, host, github, cache, clock).run(purge_cache=True)
    assert 9201 in host.machines and not cache.purged


def test_a_vm_the_controller_removed_after_the_listing_counts_as_removed(setup, tmp_path: Path) -> None:
    config, _, github, cache, clock = setup
    missing = done("", 2, "Configuration file 'nodes/pve1/qemu-server/121.conf' does not exist")
    pve = Pve(
        Script({("qm", "destroy"): missing, ("pvesh", "get", "/cluster/resources"): done([])}),
        node="pve1",
        firewall_dir=tmp_path,
    )

    class Racing(FakeHost):
        def destroy(self, vmid: int) -> None:
            # The controller retired the idle VM after teardown listed it, just before this destroy.
            del self.machines[vmid]
            pve.destroy(vmid)

    host = Racing()
    ours(host, 121, "ci-runner-121", VmState("warm", 9_000, "default", "0" * 16))
    (tmp_path / "121.fw").write_text("[OPTIONS]\n", encoding="utf-8")

    assert teardown(config, host, github, cache, clock).run(purge_cache=False) == []
    assert not (tmp_path / "121.fw").exists()
