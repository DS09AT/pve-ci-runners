import json
import logging
import os
from concurrent.futures import wait
from dataclasses import replace
from pathlib import Path

import pytest

from pve_ci_runners.actions import QueuedJob, RunSummary
from pve_ci_runners.cache import cache_key
from pve_ci_runners.capacity import HostMemory
from pve_ci_runners.config import Config, parse
from pve_ci_runners.controller import Controller, profile_for
from pve_ci_runners.drain import drain, reserve
from pve_ci_runners.errors import CommandError, GitHubError, GuestError
from pve_ci_runners.lifecycle import Lifecycle
from pve_ci_runners.overflow import Overflow
from pve_ci_runners.pve import TAG
from pve_ci_runners.reroute import Reroute
from pve_ci_runners.state import VerifiedJob, decode
from tests.conftest import config_data
from tests.fakes import HEAD, Background, FakeCache, FakeGitHub, FakeHost, FakeVm, holds_memory_lock

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
    ctl = Controller(
        config, host, github, cache, disk=host, overflow=Overflow(config.overflow, github), clock=clock
    )
    ctl._lifecycle._executor = Background(inline=True)
    ctl._lifecycle._checker = Background(inline=True)
    return ctl


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
    waiting = state(host, 121).done_at
    clock.now += 100
    ctl.cycle()
    assert 121 in host.machines and state(host, 121).done_at == waiting, (
        "the grace counts from the first miss"
    )
    clock.now += 100
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


def test_a_breach_blocks_new_work_even_when_the_vm_cannot_be_removed(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    host.machines[121].isolated = False
    host.undestroyable.add(121)

    ctl.cycle()

    assert ctl.blocked is not None


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


@pytest.mark.parametrize("running", [False, True])
def test_a_production_guest_keeps_runner_vms_out_even_while_it_is_stopped(setup, running: bool) -> None:
    config, _, github, cache, clock = setup
    host = FakeHost(total_mb=32000, other_guests_mb=0)
    host.add_template(9201)
    host.machines[100] = FakeVm(name="windows", running=running, memory_mb=26000)

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {100, 9201}


def test_a_build_vm_left_without_a_reservation_keeps_runner_vms_out(setup) -> None:
    config, _, github, cache, clock = setup
    host = FakeHost(total_mb=32000, other_guests_mb=0)
    host.add_template(9201)
    host.machines[9202] = FakeVm(name="ci-runner-template-9202", running=True, memory_mb=26000, tags={TAG})

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201, 9202}


def test_a_build_vm_counts_once_while_its_reservation_holds_its_memory(setup) -> None:
    config, _, github, cache, clock = setup
    host = FakeHost(total_mb=32000, other_guests_mb=0)
    host.add_template(9201)
    host.machines[9202] = FakeVm(name="ci-runner-template-9202", running=True, memory_mb=16000, tags={TAG})
    reserve(config.daemon.state_dir, 16000)

    controller(config, host, github, cache, clock).cycle()

    assert 121 in host.machines


@pytest.mark.parametrize(("name", "tags"), [("ci-runner-template-9202", set()), ("windows", {TAG})])
def test_a_foreign_vm_on_a_template_vmid_counts_even_during_a_build(setup, name: str, tags: set[str]) -> None:
    config, _, github, cache, clock = setup
    host = FakeHost(total_mb=32000, other_guests_mb=0)
    host.add_template(9201)
    host.machines[9202] = FakeVm(name=name, memory_mb=26000, tags=tags)
    reserve(config.daemon.state_dir, 1)

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201, 9202}


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


def test_jobs_without_a_finished_template_count_as_unserved_and_go_to_the_hosted_runner(setup) -> None:
    config, host, github, cache, clock = setup
    host.machines[9201].description = "interrupted build"
    github.queue("example-org/app", count=2)
    clock.now = 901.0

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201}
    assert github.variables[("example-org/app", "CI_RUNS_ON")] == '"ubuntu-latest"'


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


def test_disk_limits_also_apply_to_the_cache_disk(setup) -> None:
    config, host, github, cache, clock = setup
    profile = replace(config.profiles[0], disk_limits=(("iops_wr", 500), ("mbps_wr", 200)))
    limited = replace(config, profiles=(profile,))
    ctl = controller(limited, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    github.queue("example-org/app")
    ctl.cycle()
    host.machines[121].run_id = 42
    github.runs[42] = VerifiedJob(
        42, "build", ".github/workflows/ci.yml", "main", "push", same_repository=True, head_sha=HEAD
    )

    ctl.attach_pending()

    assert host.machines[121].disks["scsi1"].endswith("serial=cicache,iops_wr=500,mbps_wr=200")


def test_the_nic_rate_limits_every_runner_vm(setup) -> None:
    config, host, github, cache, clock = setup
    limited = replace(config, network=replace(config.network, rate=50))

    controller(limited, host, github, cache, clock).cycle()

    assert host.machines[121].options["net0"] == "virtio,bridge=vmbr1,firewall=1,rate=50"


def test_the_firewall_denies_the_hosts_networks_and_the_probe_tries_a_sample_of_them(setup) -> None:
    config, host, github, cache, clock = setup
    denied = replace(config, firewall=replace(config.firewall, deny_destinations=("198.51.100.0/24",)))
    ctl = controller(denied, host, github, cache, clock)
    host.networks = ()
    ctl.cycle()
    assert 121 not in host.machines, "without the host's networks no VM is cloned"

    host.networks = FakeHost().networks
    ctl.cycle()
    ctl.cycle()

    ipset = host.firewall[121].split("[IPSET deny]\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert ipset == ["198.51.100.0/24", *host.networks]
    assert state(host, 121).phase == "warm"
    assert "DENY_SAMPLE='198.51.100.1 203.0.113.1'" in host.probes[-1]


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

    assert "install -d -m 0755 -o root -g root /run/ci-runner-state" in script("prepare.sh")
    assert "ExecStopPost=+/usr/bin/touch /run/ci-runner-state/done" in script("prepare.sh")
    for name in ("finished", "report"):
        assert "/run/ci-runner-state/done" in script(f"{name}.sh") and "/run/ci-runner/done" not in script(
            f"{name}.sh"
        )


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


def test_memory_a_template_build_reserves_is_never_given_to_runners(setup) -> None:
    config, host, github, cache, clock = setup
    reserve(config.daemon.state_dir, 40000)

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201}


def test_draining_releases_idle_vms(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    ctl.cycle()
    assert state(host, 121).phase == "warm"

    drain(config.daemon.state_dir)
    ctl.cycle()

    assert 121 not in host.machines and 122 not in host.machines


@pytest.mark.parametrize("phase", ["booting", "warm"])
def test_a_running_vm_whose_out_of_memory_priority_cannot_be_set_is_never_handed_a_job(
    setup, phase: str
) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    if phase == "warm":
        ctl.cycle()
    assert state(host, 121).phase == phase
    prepared = host.scripts.count((121, "prepare"))
    host.unprotectable.add(121)
    github.queue("example-org/app")

    ctl.cycle()

    assert "destroy 121" in host.events
    assert host.scripts.count((121, "prepare")) == prepared, "removed before it is warmed up"
    assert not any(name.startswith("ci-runner-121-") for name in github.registered)


def test_an_assigned_vm_that_cannot_be_protected_gives_back_its_cache_and_registration(setup) -> None:
    _, host, github, cache, _ = setup
    ctl = started_job(setup)
    runner = state(host, 121).runner
    host.unprotectable.add(121)

    ctl.cycle()

    assert 121 not in host.machines
    assert cache.written == [] and cache.dropped == [KEY]
    assert github.removed == [runner]


def test_a_vm_restarted_outside_the_controller_is_protected_again_on_the_next_cycle(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    host.protected.clear()

    ctl.cycle()

    assert 121 in host.protected


def test_runner_vms_start_only_under_the_memory_lock_a_template_build_takes_too(setup, monkeypatch) -> None:
    config, host, github, cache, clock = setup
    locked: list[bool] = []
    start = host.start

    def recording_start(vmid: int) -> None:
        locked.append(holds_memory_lock(config.daemon.state_dir))
        start(vmid)

    monkeypatch.setattr(host, "start", recording_start)
    controller(config, host, github, cache, clock).cycle()

    assert locked == [True]
    assert not holds_memory_lock(config.daemon.state_dir)


def warm_vm(setup) -> Controller:
    """A controller whose VM 121 is warm and VM 122 is not created yet."""
    ctl = controller(*setup)
    ctl.cycle()
    ctl.cycle()
    assert state(setup[1], 121).phase == "warm"
    return ctl


def test_a_cache_that_cannot_be_attached_leaves_the_job_waiting_for_the_next_poll(setup, caplog) -> None:
    _, host, github, cache, _ = setup
    ctl = warm_vm(setup)
    github.queue("example-org/app")
    ctl.cycle()
    host.machines[121].run_id = 42
    github.runs[42] = VerifiedJob(42, "build", "ci.yml", "main", "push", same_repository=True, head_sha=HEAD)
    host.machines[121].silent = True

    ctl.attach_pending()

    assert state(host, 121).cache is None and cache.leases == []
    assert "VM 121: guest agent not running" in caplog.text
    host.machines[121].silent = False
    ctl.attach_pending()
    assert state(host, 121).cache is not None


def test_a_stopped_vm_is_kept_without_lowering_its_priority(setup) -> None:
    _, host, _, _, _ = setup
    ctl = controller(*setup)
    ctl.cycle()
    host.machines[121].running = False
    host.unprotectable.add(121)

    ctl.cycle()

    assert "destroy 121" not in host.events
    assert state(host, 121).phase == "booting"


def test_jobs_for_no_profile_are_ignored(setup) -> None:
    _, host, github, _, _ = setup
    ctl = warm_vm(setup)
    github.queue("example-org/app", labels=("self-hosted", "gpu"))

    ctl.cycle()

    assert state(host, 121).phase == "warm"
    assert github.registered == {}


def test_a_repository_github_cannot_answer_for_does_not_hold_up_the_others(
    setup, monkeypatch, caplog
) -> None:
    _, host, github, _, _ = setup
    github.repos["example-org/broken"] = "main"
    queued_jobs = github.queued_jobs

    def failing_queue(repository: str):
        if repository == "example-org/broken":
            raise GitHubError("GET /repos/example-org/broken/actions/runs answered 502: bad gateway", 502)
        return queued_jobs(repository)

    def failing_busy(_repository: str):
        raise GitHubError("GET /repos/example-org/app/actions/runners answered 502: bad gateway", 502)

    ctl = warm_vm(setup)
    monkeypatch.setattr(github, "queued_jobs", failing_queue)
    monkeypatch.setattr(github, "busy_runners", failing_busy)
    github.queue("example-org/app")

    ctl.cycle()
    ctl.cycle()

    assert state(host, 121).repository == "example-org/app"
    assert "example-org/broken: queued jobs unknown" in caplog.text
    assert "busy runners unknown" in caplog.text


def test_a_runner_that_waited_the_idle_timeout_for_a_job_is_retired(setup) -> None:
    config, host, github, _, clock = setup
    ctl = warm_vm(setup)
    github.queue("example-org/app")
    ctl.cycle()
    runner = state(host, 121).runner

    clock.now += config.daemon.idle_timeout_seconds - 1
    ctl.cycle()
    assert state(host, 121).phase == "assigned"
    clock.now += 2
    ctl.cycle()

    assert "destroy 121" in host.events
    assert github.removed == [runner]


def test_no_vm_starts_when_the_host_filled_up_since_planning(setup, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    config, host, github, cache, clock = setup
    answers = iter([host.host_memory({}), HostMemory(host.total_mb, host.total_mb)])
    monkeypatch.setattr(host, "host_memory", lambda _excluded: next(answers))

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201}
    assert "not starting VM 121: the host keeps its reserve" in caplog.text


def test_a_vm_that_cannot_be_cloned_does_not_stop_the_next_one(setup, monkeypatch) -> None:
    config, host, github, cache, clock = setup
    clone = host.clone

    def failing_clone(template: int, vmid: int, name: str) -> None:
        if vmid == 121:
            raise CommandError("qm clone 121 exited 2: storage busy")
        clone(template, vmid, name)

    monkeypatch.setattr(host, "clone", failing_clone)
    github.queue("example-org/app", count=2)
    clock.now = 901.0

    controller(config, host, github, cache, clock).cycle()

    assert set(host.machines) == {9201, 122}


def test_without_overflow_no_variable_is_written(setup) -> None:
    config, host, github, cache, clock = setup
    host.machines[9201].description = "interrupted build"
    github.queue("example-org/app", count=2)

    Controller(config, host, github, cache, disk=host, overflow=None, clock=clock).cycle()

    assert github.variables == {}


def test_maintenance_logs_what_it_did_and_a_failure_still_prunes_old_logs(setup, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    config, host, github, cache, clock = setup
    old = config.daemon.log_dir / "ci-runner-121.log"
    old.parent.mkdir(parents=True)
    old.write_text("x", encoding="utf-8")
    os.utime(old, (0, 0))
    monkeypatch.setattr(cache, "maintain", lambda _in_use: ["expired rpool/ci-cache/repos/old"])

    controller(config, host, github, cache, clock).cycle()
    assert "expired rpool/ci-cache/repos/old" in caplog.text
    assert old.exists(), "the retention counts from the controller's clock"

    def failing(_in_use: set[str]) -> list[str]:
        raise CommandError("zfs list exited 1: pool busy")

    monkeypatch.setattr(cache, "maintain", failing)
    clock.now = config.daemon.log_retention_days * 86400 + 10.0
    controller(config, host, github, cache, clock).cycle()

    assert "cache maintenance failed: zfs list exited 1: pool busy" in caplog.text
    assert not old.exists()


def test_a_vm_whose_guest_agent_is_not_up_yet_keeps_booting(setup) -> None:
    _, host, _, _, _ = setup
    ctl = controller(*setup)
    ctl.cycle()
    host.machines[121].agent = False

    ctl.cycle()
    assert state(host, 121).phase == "booting"
    assert (121, "prepare") not in host.scripts

    host.machines[121].agent = True
    ctl.cycle()
    assert state(host, 121).phase == "warm"


def test_a_runner_that_exits_without_a_verified_job_is_removed_and_its_logs_kept(setup) -> None:
    config, host, github, cache, _ = setup
    ctl = warm_vm(setup)
    github.queue("example-org/app")
    ctl.cycle()
    runner = state(host, 121).runner
    host.machines[121].done = True
    host.machines[121].journal = "runner crashed"

    ctl.cycle()

    assert 121 not in host.machines
    assert cache.leases == [] and github.removed == [runner]
    assert (config.daemon.log_dir / f"{runner}.log").read_text(encoding="utf-8") == "runner crashed"


def test_a_cache_the_guest_cannot_tidy_is_not_written_back(setup, monkeypatch) -> None:
    _, host, github, cache, _ = setup
    ctl = started_job(setup)

    def fail(_vmid: int) -> None:
        raise GuestError("guest command exited 1: umount: target is busy")

    monkeypatch.setattr(ctl._lifecycle._guest, "tidy", fail)
    host.machines[121].done = True
    github.conclusions[42] = "success"
    ctl.cycle()

    assert 121 not in host.machines
    assert cache.written == [] and cache.dropped == [KEY]


def test_a_long_cache_tidy_holds_up_neither_queued_jobs_nor_the_freed_slot(setup, monkeypatch) -> None:
    config, host, github, cache, clock = setup
    poll = config.daemon.poll_seconds
    ctl = started_job(setup)
    ctl.cycle()
    assert state(host, 122).phase == "warm"
    background = Background()
    ctl._lifecycle._executor = background

    def slow_tidy(_vmid: int) -> None:
        clock.now += 150

    monkeypatch.setattr(ctl._lifecycle._guest, "tidy", slow_tidy)
    host.machines[121].done = True
    github.conclusions[42] = "success"
    github.queue("example-org/app", count=3)
    queued_at = clock.now

    ctl.cycle()
    assert state(host, 122).phase == "assigned"
    assert state(host, 122).since - queued_at <= poll, "the warm VM takes a job while VM 121 tidies"
    assert 121 in host.machines and cache.written == []

    background.run()
    clock.now += poll
    ctl.cycle()
    assert cache.written == [KEY], "the tidied cache of the trusted run is written back"
    assert 121 not in host.machines
    freed_at = clock.now

    for _ in range(2):
        clock.now += poll
        ctl.cycle()
    assert state(host, 121).phase == "assigned"
    assert state(host, 121).since - freed_at <= 2 * poll


def test_a_tidy_started_for_one_state_never_writes_back_another(setup) -> None:
    _, host, github, cache, _ = setup
    ctl = started_job(setup)
    background = Background()
    ctl._lifecycle._executor = background
    host.machines[121].done = True
    github.conclusions[42] = "success"
    ctl.cycle()
    assert len(background.pending) == 1

    background.run()
    tidied = state(host, 121)
    assert tidied is not None and tidied.cache is not None
    forged = replace(tidied, cache=replace(tidied.cache, key="example-org/forged"))
    host.machines[121].description = forged.encode()
    ctl.cycle()

    assert 121 not in host.machines
    assert cache.written == [] and cache.dropped == ["example-org/forged"], "GitHub is asked again"


def test_the_guest_check_and_the_tidy_run_on_threads_of_their_own(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = started_job(setup)
    lifecycle = ctl._lifecycle = Lifecycle(config, host, github, cache, clock=clock)
    host.machines[121].done = True
    github.conclusions[42] = "success"

    for _ in range(8):
        ctl.cycle()
        if cache.written:
            break
        wait([f for _, f in lifecycle._checks.values()] + [f for _, f in lifecycle._tidying.values()])

    assert cache.written == [KEY] and "destroy 121" in host.events


GIB = 2**30
HOSTED = '"ubuntu-latest"'


def overflow_value(github: FakeGitHub) -> str | None:
    return github.variables.get(("example-org/app", "CI_RUNS_ON"))


def test_no_vm_starts_without_its_disk_reserve_and_the_job_overflows_at_once(setup) -> None:
    config, host, github, _, _ = setup
    host.disk_bytes = config.capacity.disk_reserve_bytes - 1
    github.queue("example-org/app")

    controller(*setup).cycle()

    assert set(host.machines) == {9201}
    assert overflow_value(github) == HOSTED, "one job, waiting briefly, overflows because no VM can start"


def test_one_job_with_disk_room_waits_for_its_vm_instead(setup) -> None:
    _, host, github, _, _ = setup
    github.queue("example-org/app")

    controller(*setup).cycle()

    assert 121 in host.machines and overflow_value(github) != HOSTED


def test_a_full_dataset_heals_by_eviction_and_the_idle_vm_takes_the_job(setup, monkeypatch) -> None:
    _, host, github, cache, _ = setup
    ctl = started_job(setup)
    ctl.cycle()
    assert state(host, 122).phase == "warm"
    host.disk_bytes = 40 * 2**20
    evictions: list[set[str]] = []

    def evict(in_use: set[str]) -> list[str]:
        evictions.append(in_use)
        host.disk_bytes = 40 * GIB
        return ["evicted the cache rpool/ci-cache/repos/a/old"]

    monkeypatch.setattr(cache, "maintain", evict)
    github.queue("example-org/app")

    ctl.cycle()

    assert evictions and state(host, 121).cache is not None
    assert evictions[0] == {state(host, 121).cache.clone}, "the running job's clone is never evicted"
    assert state(host, 122).phase == "assigned" and "destroy 122" not in host.events


def test_without_cache_to_evict_idle_vms_go_and_starts_resume_once_space_is_back(setup, caplog) -> None:
    config, host, github, _, clock = setup
    ctl = started_job(setup)
    ctl.cycle()
    runner = state(host, 121).runner
    host.disk_bytes = config.capacity.disk_reserve_bytes
    github.queue("example-org/app")

    ctl.cycle()

    assert "destroy 122" in host.events and state(host, 121).runner == runner, "only the idle VM goes"
    assert "VM 122 is retired: the disk lacks the write reserve" in caplog.text
    assert overflow_value(github) == HOSTED
    clock.now += config.daemon.poll_seconds
    ctl.cycle()
    assert 122 not in host.machines, "admission is retried, and refused, every cycle"

    host.disk_bytes = 40 * GIB
    clock.now += config.daemon.poll_seconds
    ctl.cycle()
    assert state(host, 122).phase == "booting"


def test_unknown_disk_space_starts_and_assigns_nothing_and_retires_nothing(setup, caplog) -> None:
    _, host, github, _, _ = setup
    ctl = warm_vm(setup)
    host.disk_error = True
    github.queue("example-org/app")

    ctl.cycle()

    assert state(host, 121).phase == "warm" and 122 not in host.machines
    assert "destroy 121" not in host.events
    assert "free disk space unknown" in caplog.text
    assert overflow_value(github) == HOSTED


def test_vms_start_only_as_far_as_the_disk_holds_their_reserves(setup, caplog) -> None:
    config, host, github, cache, clock = setup
    data = config_data(
        daemon={"state_dir": str(config.daemon.state_dir), "log_dir": str(config.daemon.log_dir)}
    )
    data["profile"][0]["max"] = 1
    data["profile"].append({
        **data["profile"][0],
        "name": "large",
        "match": "pve-large",
        "labels": ["self-hosted", "pve-large"],
    })
    config = parse(data)
    host.disk_bytes = config.capacity.disk_reserve_bytes

    controller(config, host, github, cache, clock).cycle()

    assert [e for e in host.events if e.startswith("clone")] == ["clone 121"]
    assert "not starting VM 122: the disk keeps the write reserve of every VM" in caplog.text


def test_a_vm_whose_guest_keeps_failing_is_retired_and_its_slot_reused(setup, caplog) -> None:
    config, host, github, cache, clock = setup
    ctl = started_job(setup)
    runner = state(host, 121).runner
    host.machines[121].silent = True
    for _ in range(config.daemon.unhealthy_checks - 1):
        clock.now += config.daemon.poll_seconds
        ctl.cycle()
        assert state(host, 121).runner == runner

    clock.now += config.daemon.poll_seconds
    ctl.cycle()

    assert "VM 121 is retired: its guest failed 3 checks in a row" in caplog.text
    assert cache.written == [] and cache.dropped == [KEY]
    assert runner in github.removed
    github.queue("example-org/app")
    clock.now += config.daemon.poll_seconds
    ctl.cycle()
    assert state(host, 121).phase == "booting", "the freed slot takes a new VM"
    assert state(host, 122).phase == "assigned"


def test_one_answered_check_resets_the_count_of_failed_ones(setup) -> None:
    _, host, _, _, _ = setup
    ctl = started_job(setup)
    for silent in (True, True, False, True, True):
        host.machines[121].silent = silent
        ctl.cycle()

    assert state(host, 121).phase == "assigned"


def test_a_warm_vm_whose_guest_fails_is_retired(setup) -> None:
    config, host, _, _, _ = setup
    ctl = warm_vm(setup)
    host.machines[121].silent = True

    for _ in range(config.daemon.unhealthy_checks):
        ctl.cycle()

    assert "destroy 121" in host.events


def checks_of(background: Background, vmid: int) -> list:
    return [work for _, work in background.pending if work.args == (vmid,)]


def test_a_guest_check_runs_in_the_background_and_the_cycle_never_waits_for_it(setup) -> None:
    _, host, github, cache, _ = setup
    ctl = started_job(setup)
    background = Background()
    ctl._lifecycle._checker = background
    host.machines[121].done = True

    ctl.cycle()
    ctl.cycle()
    assert state(host, 121).phase == "assigned" and len(checks_of(background, 121)) == 1, "one at a time"

    github.conclusions[42] = "success"
    background.run()
    ctl.cycle()
    assert 121 not in host.machines and cache.written == [KEY]


def test_a_check_started_for_an_earlier_state_is_not_used(setup) -> None:
    _, host, _, _, _ = setup
    ctl = started_job(setup)
    background = Background()
    ctl._lifecycle._checker = background
    ctl.cycle()
    current = state(host, 121)
    host.machines[121].description = current.evolve(done_at=5).encode()
    ctl.cycle()
    assert len(checks_of(background, 121)) == 1, "the same runner keeps its check"
    host.machines[121].description = current.evolve(since=current.since + 1).encode()

    ctl.cycle()

    assert len(checks_of(background, 121)) == 2, "a new check is started for another assignment"


def test_unmeasured_disk_space_holds_nothing_back(setup) -> None:
    _, host, _, _, _ = setup
    host.disk_bytes = None

    controller(*setup).cycle()

    assert 121 in host.machines


def test_an_idle_vm_that_cannot_be_retired_for_disk_room_still_takes_no_job(setup) -> None:
    config, host, github, _, _ = setup
    ctl = started_job(setup)
    ctl.cycle()
    host.undestroyable.add(122)
    host.disk_bytes = config.capacity.disk_reserve_bytes
    github.queue("example-org/app")

    ctl.cycle()

    assert state(host, 122).phase == "warm"
    assert overflow_value(github) == HOSTED


def test_a_stopped_warm_vm_is_not_asked_by_its_guest(setup) -> None:
    _, host, _, _, _ = setup
    ctl = warm_vm(setup)
    host.machines[121].running = False
    asked = host.scripts.count((121, "finished"))

    ctl.cycle()

    assert host.scripts.count((121, "finished")) == asked and state(host, 121).phase == "warm"


def test_a_running_template_build_keeps_its_disk_reserve(setup) -> None:
    config, host, _, _, _ = setup
    reserve(config.daemon.state_dir, config.template.memory_mb)
    host.disk_bytes = config.capacity.disk_reserve_bytes

    controller(*setup).cycle()

    assert 121 not in host.machines


def test_a_runner_that_takes_no_job_is_replaced_while_its_repository_still_waits(setup, caplog) -> None:
    config, host, github, cache, clock = setup
    ctl = warm_vm(setup)
    github.queue("example-org/app")
    ctl.cycle()
    stuck = state(host, 121).runner
    github.queue("example-org/app")
    clock.now += config.daemon.poll_seconds
    ctl.cycle()
    assert state(host, 122).phase == "warm", "the assigned runner covers the waiting job"

    clock.now += config.daemon.assign_timeout_seconds - config.daemon.poll_seconds
    ctl.cycle()
    assert state(host, 121).runner == stuck, "inside the assign timeout the runner keeps its job"
    clock.now += 1
    ctl.cycle()

    assert "VM 121 is retired: its runner took no job within the assign timeout" in caplog.text
    assert stuck in github.removed and cache.dropped == [] and cache.written == []
    assert state(host, 122).phase == "assigned", "a fresh VM takes the waiting job"


def test_a_runner_github_reports_busy_is_never_retired_as_stuck(setup) -> None:
    config, host, github, _, clock = setup
    ctl = warm_vm(setup)
    github.queue("example-org/app")
    ctl.cycle()
    github.busy.add(state(host, 121).runner)
    github.queue("example-org/app")

    clock.now += config.daemon.assign_timeout_seconds + 1
    ctl.cycle()

    assert "destroy 121" not in host.events


def test_a_short_disk_evicts_in_the_same_cycle_and_the_job_gets_its_vm(setup, monkeypatch) -> None:
    _, host, github, cache, _ = setup
    host.disk_bytes = 0

    def evict(_in_use: set[str]) -> list[str]:
        host.disk_bytes = 40 * GIB
        return ["evicted the cache rpool/ci-cache/repos/a/old"]

    monkeypatch.setattr(cache, "maintain", evict)
    github.queue("example-org/app")

    controller(*setup).cycle()

    assert 121 in host.machines
    assert overflow_value(github) != HOSTED


def test_a_short_disk_without_caches_to_evict_overflows_and_recovers_by_itself(setup, monkeypatch) -> None:
    config, host, github, cache, clock = setup
    host.disk_bytes = 0
    evictable = [False]
    calls: list[int] = []

    def evict(_in_use: set[str]) -> list[str]:
        calls.append(1)
        if evictable[0]:
            host.disk_bytes = 40 * GIB
        return []

    monkeypatch.setattr(cache, "maintain", evict)
    github.queue("example-org/app")
    ctl = controller(*setup)

    ctl.cycle()
    assert set(host.machines) == {9201} and overflow_value(github) == HOSTED
    tried = len(calls)

    clock.now += config.daemon.poll_seconds
    ctl.cycle()
    assert len(calls) == tried + 1 and 121 not in host.machines, "eviction is retried every cycle"

    evictable[0] = True
    for _ in range(8):
        clock.now += config.daemon.poll_seconds
        ctl.cycle()
    assert state(host, 121).phase == "assigned"
    assert overflow_value(github) != HOSTED, "overflow switches off once VMs start again"


def test_a_finished_runner_whose_tidy_takes_minutes_keeps_its_write_back(setup) -> None:
    config, host, github, cache, clock = setup
    ctl = started_job(setup)
    checker = ctl._lifecycle._checker = Background()
    tidy = ctl._lifecycle._executor = Background()
    host.machines[121].done = True
    github.conclusions[42] = "success"
    tidy_seconds = 300

    for _ in range(tidy_seconds // config.daemon.poll_seconds):
        ctl.cycle()
        checker.run()
        clock.now += config.daemon.poll_seconds
    assert 121 in host.machines and len(tidy.pending) == 1, "one tidy, and the VM waits for it"

    tidy.run()
    ctl.cycle()

    assert 121 not in host.machines and cache.written == [KEY] and cache.dropped == []


def test_no_disk_room_starts_nothing_and_counts_each_job_once(setup, monkeypatch, caplog) -> None:
    _, host, github, _, _ = setup
    host.disk_error = True
    github.queue("example-org/app")
    ctl = controller(*setup)
    assert ctl._overflow is not None
    reported: list[int] = []
    update = ctl._overflow.update

    def spy(**kwargs):
        reported.append(kwargs["unserved"])
        return update(**kwargs)

    monkeypatch.setattr(ctl._overflow, "update", spy)

    ctl.cycle()

    assert reported == [1]
    assert "not starting VM" not in caplog.text and set(host.machines) == {9201}


def test_overflow_switches_on_while_a_slot_is_still_free_and_off_once_room_lasts(setup, caplog) -> None:
    config, host, github, cache, clock = setup
    assert config.overflow is not None
    thresholds = replace(config.overflow, on_free_slots=1, off_free_slots=2, off_seconds=300)
    setup = (replace(config, overflow=thresholds), host, github, cache, clock)
    caplog.set_level(logging.INFO, logger="pve_ci_runners")

    ctl = started_job(setup)
    assert overflow_value(github) == HOSTED, "runs created now would lock onto the last slot"
    assert "overflow on: 1 slot free" in caplog.text

    host.machines[121].done = True
    github.conclusions[42] = "success"
    ctl.cycle()
    assert 121 not in host.machines
    for _ in range(300 // config.daemon.poll_seconds):
        clock.now += config.daemon.poll_seconds
        ctl.cycle()
        assert overflow_value(github) == HOSTED, "the pool must have room for a while first"
    clock.now += config.daemon.poll_seconds
    ctl.cycle()

    assert overflow_value(github) != HOSTED
    assert "overflow off: 2 slots free and nothing unserved for 300 s" in caplog.text


def rerouting(
    config: Config, host: FakeHost, github: FakeGitHub, cache: FakeCache, clock: Clock
) -> Controller:
    assert config.overflow is not None
    ctl = controller(config, host, github, cache, clock)
    ctl._reroute = Reroute(config.overflow, github)
    return ctl


STALLED = QueuedJob(frozenset({"self-hosted", "pve-ci"}), 800.0, 7, RunSummary(True, "push", True))


def test_a_burst_bound_to_the_pool_is_re_run_once_overflow_is_on(setup, caplog) -> None:
    caplog.set_level(logging.INFO)
    config, host, github, _, clock = setup
    host.disk_bytes = config.capacity.disk_reserve_bytes - 1
    github.queued["example-org/app"] = [STALLED] * 4
    ctl = rerouting(*setup)

    ctl.cycle()
    assert overflow_value(github) == HOSTED and github.cancelled == [], "the variable was not hosted yet"

    clock.now += 15
    ctl.cycle()
    assert github.cancelled == [7] and github.reruns == []

    clock.now += 15
    ctl.cycle()
    assert github.reruns == [7]
    assert "rerouted example-org/app run 7: 4 jobs waited 215 s, discarded 0 running jobs" in caplog.text


def test_a_repository_whose_warm_vm_is_assigned_in_the_same_cycle_is_not_rerouted(setup) -> None:
    _, host, github, _, clock = setup
    ctl = rerouting(*setup)
    ctl.cycle()
    ctl.cycle()
    assert state(host, 121).phase == "warm"
    assert ctl._overflow is not None
    ctl._overflow.update(unserved=5, oldest_wait=0, free=0, now=clock.now)
    assert overflow_value(github) == HOSTED
    github.queued["example-org/app"] = [STALLED] * 5

    ctl.cycle()
    assert state(host, 121).phase == "assigned", "the longest waiting job gets the warm VM"
    clock.now += 15
    ctl.cycle()

    assert github.cancelled == [], "an assigned runner that has not started may still take a job of the run"


def test_nothing_is_rerouted_while_every_queued_job_has_a_vm(setup) -> None:
    _, _, github, _, clock = setup
    ctl = rerouting(*setup)
    assert ctl._overflow is not None
    ctl._overflow.update(unserved=5, oldest_wait=0, free=0, now=clock.now)
    github.queued["example-org/app"] = [STALLED]

    ctl.cycle()

    assert github.cancelled == [], "the VM booting for the job serves it"


def test_nothing_is_rerouted_while_overflow_is_off(tmp_path: Path) -> None:
    data = config_data(daemon={"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs")})
    data["overflow"]["on_wait_seconds"] = 600
    config, host, github, cache, clock = parse(data), FakeHost(), FakeGitHub(), FakeCache(), Clock()
    host.add_template(9201)
    github.queued["example-org/app"] = [STALLED]

    rerouting(config, host, github, cache, clock).cycle()

    assert overflow_value(github) != HOSTED and github.cancelled == []


def test_without_reroute_the_controller_cancels_nothing(setup) -> None:
    _, _, github, _, _ = setup
    github.queued["example-org/app"] = [STALLED] * 4

    controller(*setup).cycle()

    assert overflow_value(github) == HOSTED and github.cancelled == []


def test_the_capacity_variable_carries_the_spare_slots_and_zero_while_no_vm_can_start(setup) -> None:
    config, host, github, cache, clock = setup
    assert config.overflow is not None
    config = replace(config, overflow=replace(config.overflow, capacity_variable="KB_FREE_SLOTS"))

    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    assert github.variables[("example-org/app", "KB_FREE_SLOTS")] == "2", "a booting VM and a free slot"

    host.disk_bytes = 0
    github.queue("example-org/app")
    clock.now += 15
    ctl.cycle()
    assert github.variables[("example-org/app", "KB_FREE_SLOTS")] == "0"


def test_without_a_capacity_variable_the_controller_writes_only_the_overflow_variable(setup) -> None:
    _, _, github, _, _ = setup

    controller(*setup).cycle()

    assert set(github.variables) == {("example-org/app", "CI_RUNS_ON")}


def published(github: FakeGitHub) -> str | None:
    return github.variables.get(("example-org/app", "KB_FREE_SLOTS"))


def recycling(setup, *, maximum: int = 2) -> tuple[Controller, FakeHost, FakeGitHub, Clock, Config]:
    """Every VM of the profile runs a job, and the job of VM 121 has just ended."""
    config, host, github, cache, clock = setup
    assert config.overflow is not None
    profile = replace(config.profiles[0], maximum=maximum)
    overflow = replace(config.overflow, capacity_variable="KB_FREE_SLOTS", off_free_slots=1)
    config = replace(config, profiles=(profile,), overflow=overflow)
    ctl = controller(config, host, github, cache, clock)
    for _ in range(maximum + 1):
        ctl.cycle()
        github.queue("example-org/app")
    ctl.cycle()
    assert {state(host, v).phase for v in host.machines if v != 9201} == {"assigned"}
    host.machines[121].run_id = 42
    github.runs[42] = VerifiedJob(42, "build", "ci.yml", "main", "push", same_repository=True, head_sha=HEAD)
    ctl.attach_pending()
    host.machines[121].done = True
    github.queued.clear()
    clock.now += 15
    return ctl, host, github, clock, config


def test_a_slot_being_recycled_counts_as_free_capacity_but_not_for_the_overflow_decision(setup) -> None:
    ctl, host, github, _, _ = recycling(setup)

    ctl.cycle()

    assert state(host, 121).phase == "assigned", "the job's result is not recorded yet"
    assert published(github) == "1"
    assert overflow_value(github) == HOSTED, "the decision still sees no free slot"


def test_a_slot_being_recycled_publishes_nothing_while_overflow_is_forced_on(setup) -> None:
    ctl, _, github, _, config = recycling(setup)
    drain(config.daemon.state_dir)

    ctl.cycle()

    assert published(github) == "0"


def test_a_slot_being_recycled_counts_only_as_far_as_the_profile_max_allows(setup) -> None:
    ctl, host, github, _, _ = recycling(setup, maximum=1)
    assert 122 not in host.machines

    ctl.cycle()

    assert published(github) == "1", "two slots, but the profile holds one VM"


@pytest.mark.parametrize("short", ["memory", "disk"])
def test_a_slot_being_recycled_counts_only_if_memory_and_disk_admit_a_new_vm(setup, short: str) -> None:
    ctl, host, github, _, config = recycling(setup)
    if short == "memory":
        host.other_guests_mb = host.total_mb
    else:
        host.disk_bytes = 2 * config.capacity.disk_reserve_bytes - 1

    ctl.cycle()

    assert published(github) == "0"


@pytest.mark.parametrize("short", ["memory", "disk"])
def test_a_vm_removed_this_cycle_frees_its_slot_but_not_memory_or_disk_a_second_time(
    setup, short: str
) -> None:
    ctl, host, github, _, config = recycling(setup)
    github.conclusions[42] = "failure"
    if short == "memory":
        # After the removal exactly no further VM fits; the removed VM's memory is already back.
        limit = host.total_mb - config.capacity.host_reserve_mb
        host.other_guests_mb = limit - 2 * config.profiles[0].memory_mb + 1
    else:
        host.disk_bytes = config.capacity.disk_reserve_bytes * 1

    ctl.cycle()

    assert 121 not in host.machines, "the job's result is recorded, so the VM is removed at once"
    assert published(github) == "0", "the room left after the removal holds no further VM"


@pytest.mark.parametrize("wrapped", [1, 2])
def test_an_unmeasured_disk_publishes_no_capacity_however_many_vms_are_recycled(
    tmp_path: Path, wrapped: int
) -> None:
    data = config_data(
        daemon={"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs")},
        capacity={"memory_mb": 3 * 5120, "host_reserve_mb": 4096},
    )
    data["slot"] = [{"vmid": 121 + i, "address": f"192.168.100.{21 + i}"} for i in range(3)]
    data["profile"][0]["max"] = 3
    data["overflow"]["capacity_variable"] = "KB_FREE_SLOTS"
    config, host, github, cache, clock = parse(data), FakeHost(), FakeGitHub(), FakeCache(), Clock()
    host.add_template(9201)
    ctl = controller(config, host, github, cache, clock)
    ctl.cycle()
    jobs = [121 + i for i in range(wrapped)]
    for vmid in jobs:
        ctl.cycle()
        github.queue("example-org/app")
        ctl.cycle()
        # A started job keeps the next queued one from counting as covered by this runner.
        host.machines[vmid].run_id = vmid
        github.runs[vmid] = VerifiedJob(
            vmid, "b", "ci.yml", "main", "push", same_repository=True, head_sha=HEAD
        )
        ctl.attach_pending()
    ctl.cycle()
    assert [v for v in sorted(host.machines) if v != 9201 and state(host, v).phase == "assigned"] == jobs
    assert state(host, 121 + wrapped).phase == "warm"
    for vmid in jobs:
        host.machines[vmid].done = True
    host.disk_error = True
    clock.now += 15

    ctl.cycle()

    assert all(state(host, v).phase == "assigned" for v in jobs), "their results are not recorded yet"
    assert published(github) == "0"
