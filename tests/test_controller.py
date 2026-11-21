import json
from dataclasses import replace
from pathlib import Path

import pytest

from pve_ci_runners.cache import cache_key
from pve_ci_runners.config import Config, parse
from pve_ci_runners.controller import Controller, profile_for
from pve_ci_runners.drain import drain
from pve_ci_runners.errors import CommandError
from pve_ci_runners.overflow import Overflow
from pve_ci_runners.pve import TAG
from pve_ci_runners.state import VerifiedJob, decode
from tests.conftest import config_data
from tests.fakes import HEAD, FakeCache, FakeGitHub, FakeHost, FakeVm

KEY = cache_key("example-org/app", "ci.yml", "build")


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup(tmp_path: Path):
    data = config_data(daemon={"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs")})
    config = parse(data)
    host = FakeHost()
    host.add_template(9201)
    github = FakeGitHub()
    cache = FakeCache()
    clock = Clock()
    return config, host, github, cache, clock


def controller(
    config: Config, host: FakeHost, github: FakeGitHub, cache: FakeCache, clock: Clock
) -> Controller:
    assert config.overflow is not None
    return Controller(config, host, github, cache, overflow=Overflow(config.overflow, github), clock=clock)


def state(host: FakeHost, vmid: int):
    return decode(host.machines[vmid].description, running=host.machines[vmid].running)


def test_a_job_runs_in_a_fresh_vm_and_its_cache_is_written_back(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)

    ctl.cycle()
    assert set(host.machines) == {9201, 121}, "one VM boots for the warm pool"
    assert host.events[:2] == ["clone 121", "start 121"]
    assert "policy_in: DROP" in host.firewall[121]
    assert host.machines[121].options["cpuunits"] == "100"
    assert TAG in host.machines[121].tags

    ctl.cycle()
    assert state(host, 121).phase == "warm"
    assert (121, "probe") in host.scripts, "isolation is proven before the VM is warm"

    github.queue("example-org/app")
    ctl.cycle()
    assigned = state(host, 121)
    assert assigned.phase == "assigned" and assigned.repository == "example-org/app"
    assert 122 in host.machines, "the warm pool is refilled"

    host.machines[121].run_id = 42
    github.runs[42] = VerifiedJob(
        42, "build", ".github/workflows/ci.yml", "main", "push", same_repository=True, head_sha=HEAD
    )
    ctl.attach_pending()
    attached = state(host, 121)
    key = cache_key("example-org/app", ".github/workflows/ci.yml", "build")
    assert attached.cache is not None and attached.cache.key == key
    assert host.machines[121].disks["scsi1"].endswith("serial=cicache")

    host.machines[121].done = True
    github.conclusions[42] = "success"
    ctl.cycle()
    assert 121 not in host.machines
    assert cache.written == [key]
    assert github.removed == [attached.runner], "the VM's own registration is removed"


def test_pull_request_jobs_never_write_the_cache_and_failures_keep_logs(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    github.queue("example-org/app")
    ctl.cycle()
    host.machines[121].run_id = 7
    github.runs[7] = VerifiedJob(
        7, "build", "ci.yml", "feature", "pull_request", same_repository=True, head_sha=HEAD
    )
    ctl.attach_pending()

    host.machines[121].done = True
    github.conclusions[7] = "failure"
    runner = state(host, 121).runner
    ctl.cycle()

    assert cache.written == []
    assert cache.dropped == [KEY]
    log = config.daemon.log_dir / f"{runner}.log"
    assert log.read_text(encoding="utf-8") == "journal"
    assert log.stat().st_mode & 0o777 == 0o600


def test_a_job_the_api_does_not_confirm_gets_no_cache(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    github.queue("example-org/app")
    ctl.cycle()

    host.machines[121].run_id = 999
    ctl.attach_pending()

    assert state(host, 121).cache is None
    assert (121, "release") in host.scripts, "the job continues without a cache"
    assert cache.leases == []


def test_a_restart_in_the_middle_of_a_job_converges(setup) -> None:
    config, host, github, cache, clock = setup
    first = controller(config, host, github, cache, clock)
    first.cycle()
    first.cycle()
    github.queue("example-org/app")
    first.cycle()
    host.machines[121].run_id = 42
    github.runs[42] = VerifiedJob(42, "build", "ci.yml", "main", "push", same_repository=True, head_sha=HEAD)
    first.attach_pending()

    second = controller(config, host, github, cache, clock)
    second.attach_pending()
    assert len(cache.leases) == 1, "the cache is not attached twice"
    host.machines[121].done = True
    github.conclusions[42] = "success"
    second.cycle()

    assert 121 not in host.machines
    assert cache.written == [KEY]


def test_the_result_is_awaited_before_a_finished_vm_is_removed(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    github.queue("example-org/app")
    ctl.cycle()
    host.machines[121].run_id = 42
    github.runs[42] = VerifiedJob(42, "build", "ci.yml", "main", "push", same_repository=True, head_sha=HEAD)
    ctl.attach_pending()
    host.machines[121].done = True

    ctl.cycle()
    assert 121 in host.machines, "GitHub has not recorded the result yet"
    clock.now += 200
    ctl.cycle()
    assert 121 not in host.machines, "after the grace period the VM goes, without writing back"
    assert cache.written == []


def test_a_vm_that_reaches_the_hypervisor_blocks_all_new_work(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    host.machines[121].isolated = False

    ctl.cycle()

    assert 121 not in host.machines
    assert ctl.blocked is not None
    github.queue("example-org/app")
    ctl.cycle()
    assert set(host.machines) == {9201}, "nothing is created while blocked"
    assert github.variables[("example-org/app", "CI_RUNS_ON")] == '"ubuntu-latest"', (
        "jobs go to the hosted runner"
    )


def test_foreign_and_unnamed_vms_in_a_slot_are_never_touched(setup) -> None:
    config, host, github, cache, clock = setup
    host.machines[121] = FakeVm(name="someone-elses-vm", running=True, tags={TAG})
    host.machines[122] = FakeVm(name="ci-runner-122", running=True)
    ctl = controller(config, host, github, cache, clock)

    ctl.cycle()

    assert host.machines[121].name == "someone-elses-vm"
    assert host.machines[122].name == "ci-runner-122"
    assert "destroy 121" not in host.events and "destroy 122" not in host.events


def test_a_vm_with_a_corrupted_state_is_removed(setup) -> None:
    config, host, github, cache, clock = setup
    host.machines[121] = FakeVm(name="ci-runner-121", running=True, tags={TAG}, description="{not json")
    controller(config, host, github, cache, clock).cycle()

    assert "destroy 121" in host.events


def test_the_host_reserve_keeps_runner_vms_out(setup) -> None:
    config, _, github, cache, clock = setup
    host = FakeHost(total_mb=32000, other_guests_mb=26000)
    host.add_template(9201)

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201}


def test_draining_takes_no_new_jobs_and_sends_them_to_the_hosted_runner(setup) -> None:
    config, host, github, cache, clock = setup
    drain(config.daemon.state_dir)
    github.queue("example-org/app")

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201}
    assert github.variables[("example-org/app", "CI_RUNS_ON")] == '"ubuntu-latest"'


def test_no_vm_is_created_without_a_finished_template(setup) -> None:
    config, host, github, cache, clock = setup
    host.machines[9201].description = "interrupted build"

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201}


def test_warm_vms_of_an_older_payload_are_replaced(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    warm = json.loads(host.machines[121].description)
    host.machines[121].description = json.dumps(warm | {"payload": "ffffffffffffffff"})

    ctl.cycle()

    assert "destroy 121" in host.events


def test_a_vm_that_never_boots_is_removed(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    host.machines[121].agent = False
    clock.now += config.daemon.boot_timeout_seconds + 1

    ctl.cycle()

    assert "destroy 121" in host.events


def test_disk_limits_are_applied_to_the_root_disk(setup) -> None:
    config, host, github, cache, clock = setup
    profile = replace(config.profiles[0], disk_limits=(("mbps_wr", 200),))
    limited = replace(config, profiles=(profile,))

    controller(limited, host, github, cache, clock).cycle()

    assert host.machines[121].disks["scsi0"].endswith(",mbps_wr=200")


def test_jobs_pick_their_profile_by_the_match_label(config: Config) -> None:
    assert profile_for(frozenset({"self-hosted", "pve-ci"}), config.profiles) is config.profiles[0]
    assert profile_for(frozenset({"self-hosted", "PVE-CI", "linux"}), config.profiles) is config.profiles[0]
    assert profile_for(frozenset({"self-hosted", "pve-ci", "gpu"}), config.profiles) is None
    assert profile_for(frozenset({"ubuntu-latest"}), config.profiles) is None


def started_job(setup, run: VerifiedJob | None = None) -> Controller:
    """A controller whose VM 121 runs a verified job of example-org/app with a cache attached."""
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    github.queue("example-org/app")
    ctl.cycle()
    host.machines[121].run_id = 42
    github.runs[42] = run or VerifiedJob(
        42, "build", "ci.yml", "main", "push", same_repository=True, head_sha=HEAD
    )
    ctl.attach_pending()
    assert state(host, 121).cache is not None
    return ctl


@pytest.mark.parametrize("silent", [False, True])
def test_a_vm_that_outlives_the_longest_job_is_removed_without_write_back(setup, silent: bool) -> None:
    config, host, github, cache, clock = setup
    ctl = started_job(setup)
    host.machines[121].silent = silent
    clock.now += config.daemon.max_job_seconds - 1
    ctl.cycle()
    assert 121 in host.machines, "a job inside the limit keeps its VM"

    clock.now += 2
    github.conclusions[42] = "success"
    ctl.cycle()

    assert 121 not in host.machines
    assert cache.written == [] and cache.dropped == [KEY]
    assert github.removed, "the registration is removed"


def test_a_job_github_recorded_as_ended_is_removed_although_the_guest_stays_silent(setup) -> None:
    _, host, github, cache, clock = setup
    ctl = started_job(setup)
    host.machines[121].silent = True
    github.conclusions[42] = "success"

    ctl.cycle()
    assert 121 in host.machines, "the guest gets a grace period to report its end"
    clock.now += 200
    ctl.cycle()

    assert 121 not in host.machines
    assert cache.written == [], "a guest that never confirmed its end may still be writing its cache"


def test_the_runner_exit_marker_lives_where_the_job_user_cannot_write() -> None:
    from pve_ci_runners.guest import script

    assert "install -d -m 0755 -o root -g root /run/ci-runner-state" in script("prepare")
    assert "ExecStopPost=+/usr/bin/touch /run/ci-runner-state/done" in script("prepare")
    for name in ("finished", "report"):
        assert "/run/ci-runner-state/done" in script(name) and "/run/ci-runner/done" not in script(name)


def finish_job(setup, conclusion: str = "success") -> None:
    _, host, github, _, _ = setup
    host.machines[121].done = True
    github.conclusions[42] = conclusion
    controller(*setup).cycle()
    assert 121 not in host.machines


def forge(host: FakeHost, **changes: object) -> None:
    host.machines[121].description = json.dumps(json.loads(host.machines[121].description) | changes)


def test_a_rewritten_description_cannot_turn_a_pull_request_into_a_write_back(setup) -> None:
    _, host, _, cache, _ = setup
    started_job(
        setup, VerifiedJob(42, "build", "ci.yml", "pr", "pull_request", same_repository=True, head_sha=HEAD)
    )
    verified = json.loads(host.machines[121].description)["verified"]
    forge(host, verified=verified | {"branch": "main", "event": "push"})

    finish_job(setup)

    assert cache.written == [] and cache.dropped == [KEY]


def test_a_rewritten_cache_key_is_not_written_back(setup) -> None:
    _, host, _, cache, _ = setup
    started_job(setup)
    lease = json.loads(host.machines[121].description)["cache"]
    forged = cache_key("example-org/other", "ci.yml", "build")
    forge(host, cache=lease | {"key": forged})

    finish_job(setup)

    assert cache.written == [] and cache.dropped == [forged]


def test_a_runner_name_the_controller_did_not_derive_for_the_vm_is_not_ours(setup) -> None:
    _, host, github, _, _ = setup
    started_job(setup)
    forge(host, runner="ci-runner-122-abcdef")

    controller(*setup).cycle()

    assert "destroy 121" in host.events
    assert github.removed == [], "no registration named by a forged state is deleted"


@pytest.mark.parametrize("head", ["f" * 40, None])
def test_a_run_that_is_not_the_default_branch_head_is_not_written_back(setup, head: str | None) -> None:
    _, _, github, cache, _ = setup
    started_job(setup)
    if head is None:
        del github.heads["example-org/app"]
    else:
        github.heads["example-org/app"] = head

    finish_job(setup)

    assert cache.written == [] and cache.dropped == [KEY]


def test_an_unknown_default_branch_never_writes_back(setup) -> None:
    _, _, github, cache, clock = setup
    started_job(setup)
    github.repos = {}
    clock.now += 601

    finish_job(setup)

    assert cache.written == [] and cache.dropped == [KEY]


def test_the_probe_tries_every_host_address_and_an_unknown_one_keeps_the_vm_cold(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    host.addresses = ()
    ctl.cycle()
    assert state(host, 121).phase == "booting", "no host address list, no proof, no warm VM"

    host.addresses = ("192.168.100.1", "203.0.113.10", "2001:db8::10")
    ctl.cycle()

    assert state(host, 121).phase == "warm"
    assert "HOST_ADDRESSES='192.168.100.1 203.0.113.10 2001:db8::10'" in host.probes[-1]


def test_kept_logs_are_capped_to_their_last_mebibyte(setup) -> None:
    config, host, _, _, _ = setup
    started_job(setup)
    runner = state(host, 121).runner
    host.machines[121].journal = "x" * 2**21 + "end"

    finish_job(setup, "failure")

    kept = (config.daemon.log_dir / f"{runner}.log").read_bytes()
    assert len(kept) == 2**20 and kept.endswith(b"end")


TRUSTED = VerifiedJob(41, "build", "ci.yml", "main", "push", same_repository=True, head_sha=HEAD)
EARLIER = "ci-runner-121-abcdef"


def test_a_finished_trusted_job_of_an_earlier_vm_in_the_slot_gets_no_cache(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    github.queue("example-org/app")
    ctl.cycle()
    github.registered[EARLIER] = "example-org/app"
    github.runs[41] = TRUSTED
    github.conclusions[41] = "success"
    forge(host, runner=EARLIER)
    host.machines[121].run_id = 41

    ctl.attach_pending()

    assert cache.leases == []
    assert state(host, 121).cache is None


def test_a_clone_is_not_written_back_for_another_run_named_in_the_description(setup) -> None:
    _, host, github, cache, _ = setup
    started_job(
        setup, VerifiedJob(42, "build", "ci.yml", "pr", "pull_request", same_repository=True, head_sha=HEAD)
    )
    github.registered[EARLIER] = "example-org/app"
    github.runs[41] = TRUSTED
    github.conclusions[41] = "success"
    verified = json.loads(host.machines[121].description)["verified"]
    forge(host, runner=EARLIER, verified=verified | {"run_id": 41, "branch": "main", "event": "push"})

    finish_job(setup)

    assert cache.written == [] and cache.dropped == [KEY]


def test_a_description_naming_an_unserved_repository_deletes_no_registration(setup) -> None:
    _, host, github, cache, _ = setup
    started_job(setup)
    forge(host, repository="example-org/other")

    finish_job(setup)

    assert github.removed == []
    assert cache.written == [] and cache.dropped == [KEY]


def test_a_failed_write_back_still_drops_the_clone_and_removes_the_registration(setup, monkeypatch) -> None:
    _, host, github, cache, _ = setup
    started_job(setup)
    runner = state(host, 121).runner

    def fail(*_args: object, **_kwargs: object) -> bool:
        raise CommandError("zfs promote exited 1: out of space")

    monkeypatch.setattr(cache, "write_back", fail)
    finish_job(setup)

    assert cache.dropped == [KEY]
    assert github.removed == [runner]
