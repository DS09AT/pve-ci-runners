import re
import shlex
import subprocess
from pathlib import Path

import pytest

from pve_ci_runners.cache import MAX_KEY_LENGTH, NoCache, cache_key
from pve_ci_runners.capacity import HostMemory, vms_that_fit
from pve_ci_runners.config import Config
from pve_ci_runners.errors import GuestError
from pve_ci_runners.firewall import render
from pve_ci_runners.guest import parse_run_id, payload_version, script, with_inputs
from pve_ci_runners.promotion import may_write_back, worth_keeping
from pve_ci_runners.state import CacheLease, VerifiedJob


def test_the_host_reserve_and_the_budget_both_bound_new_vms() -> None:
    host = HostMemory(total_mb=64000, committed_mb=24000)

    assert vms_that_fit(host, reserve_mb=4000, budget_left_mb=20480, vm_mb=5120) == 4
    assert vms_that_fit(host, reserve_mb=34000, budget_left_mb=20480, vm_mb=5120) == 1
    assert vms_that_fit(host, reserve_mb=40000, budget_left_mb=20480, vm_mb=5120) == 0
    with pytest.raises(ValueError, match="positive"):
        vms_that_fit(host, reserve_mb=0, budget_left_mb=1, vm_mb=0)


@pytest.mark.parametrize("unit", ["pve-ci-runners.service", "pve-ci-runners-template.service"])
def test_both_units_start_only_after_the_onboot_guests(unit: str) -> None:
    lines = Path("packaging", unit).read_text(encoding="utf-8").splitlines()
    after = [word for line in lines if line.startswith("After=") for word in line[6:].split()]

    assert "pve-guests.service" in after
    assert not any("pve-guests" in line for line in lines if line.startswith(("Wants=", "Requires=")))


def test_the_firewall_denies_inbound_private_and_ipv6_and_pins_the_address(config: Config) -> None:
    text = render(config.slots[0], config.firewall)

    assert "policy_in: DROP" in text
    assert "[IPSET ipfilter-net0]\n192.168.100.21\n" in text
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "::/0"):
        assert f"OUT DROP -dest {cidr}" in text
    assert text.index("-dport 22 # git over SSH") < text.index("OUT DROP -p tcp -dport 22")


def test_no_ssh_destination_reopens_a_private_one(config: Config) -> None:
    from dataclasses import replace

    text = render(config.slots[0], replace(config.firewall, ssh_destinations=("0.0.0.0/0",)))

    ssh = text.index("OUT ACCEPT -dest 0.0.0.0/0 -p tcp -dport 22")
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "::/0"):
        assert text.index(f"OUT DROP -dest {cidr} ") < ssh


def test_extra_rules_come_before_the_denials(config: Config) -> None:
    from dataclasses import replace

    firewall = replace(config.firewall, extra_rules=("OUT ACCEPT -dest 10.0.0.5 -p tcp -dport 443",))
    text = render(config.slots[0], firewall)

    assert text.index("OUT ACCEPT -dest 10.0.0.5") < text.index("OUT DROP -dest 10.0.0.0/8")


def test_cache_keys_stay_inside_their_repository() -> None:
    key = cache_key("Example-Org/app", ".github/workflows/ci.yml", "build")
    assert re.fullmatch(r"example-org__app-[0-9a-f]{32}/ci__build-[0-9a-f]{32}", key)
    assert key == cache_key("example-org/APP", ".github/workflows/ci.yml", "build")
    key = cache_key("o/r", "x", "../../evil/key")
    assert "/" not in key.split("/", 1)[1]
    assert ".." not in key
    assert re.fullmatch(r"o__r-[0-9a-f]{32}/unnamed__unnamed-[0-9a-f]{32}", cache_key("o/r", "", ""))


def test_cache_keys_never_collide_after_cleaning_or_truncation() -> None:
    long_a, long_b = "o/" + "a" * 90 + "x", "o/" + "a" * 90 + "y"
    pairs = [
        (("org/app", "ci.yml", "build"), ("org/app-", "ci.yml", "build")),
        (("org/app", "ci.yml", "build"), ("org/app.", "ci.yml", "build")),
        ((long_a, "ci.yml", "build"), (long_b, "ci.yml", "build")),
        (("o/r", "ci.yml", "test (3.12)"), ("o/r", "ci.yml", "test-3.12-")),
        (("o/r", "ci.yml", "b" * 90 + "1"), ("o/r", "ci.yml", "b" * 90 + "2")),
        (("o/r", "a/ci.yml", "build"), ("o/r", "b/ci.yml", "build")),
    ]
    for first, second in pairs:
        assert cache_key(*first) != cache_key(*second)
    for key in (cache_key(long_a, "w" * 300, "j" * 300), cache_key("o/r", "ci.yml", "build")):
        assert re.fullmatch(r"[a-z0-9_.-]{1,65}/[a-z0-9_.-]{1,99}", key), "fits the stored state"
        assert len(key) <= MAX_KEY_LENGTH


def test_only_trusted_default_branch_successes_write_back() -> None:
    head = "a" * 40
    good = VerifiedJob(1, "build", "ci.yml", "main", "push", same_repository=True, head_sha=head)
    rule = {"default_branch": "main", "default_head": head}

    assert may_write_back(good, conclusion="success", **rule)
    assert not may_write_back(good, conclusion="failure", **rule)
    assert not may_write_back(good, conclusion="success", default_branch="develop", default_head=head)
    for event in ("pull_request", "pull_request_target", "workflow_run"):
        assert not may_write_back(
            VerifiedJob(1, "b", "ci.yml", "main", event, same_repository=True, head_sha=head),
            conclusion="success",
            **rule,
        )
    fork = VerifiedJob(1, "b", "ci.yml", "main", "push", same_repository=False, head_sha=head)
    assert not may_write_back(fork, conclusion="success", **rule)


def test_a_tag_named_like_the_default_branch_does_not_write_back() -> None:
    tag = VerifiedJob(1, "build", "ci.yml", "main", "push", same_repository=True, head_sha="b" * 40)

    assert not may_write_back(tag, conclusion="success", default_branch="main", default_head="a" * 40)
    assert worth_keeping(2**20, min_write_mb=1)
    assert not worth_keeping(2**20 - 1, min_write_mb=1)


def test_the_no_cache_backend_keeps_nothing() -> None:
    backend = NoCache()
    lease = CacheLease("k/k", "a/b", "a/b@c")

    assert backend.lease("k/k", 1, runner="r", run_id=1) is None
    assert backend.written_bytes(lease, 1) == 0
    assert not backend.write_back(lease, 1, runner="r", run_id=1)
    backend.drop(lease, 1)
    assert backend.maintain(set()) == []


def test_guest_inputs_are_quoted_so_values_stay_data() -> None:
    text = with_inputs("release", {"HOSTNAME": "x'; rm -rf / #", "DNS": "192.0.2.1 192.0.2.2"})
    first_line = text.splitlines()[0]

    assert shlex.split(first_line) == ["DNS=192.0.2.1 192.0.2.2"]
    assert "HOSTNAME='x'\"'\"'; rm -rf / #'" in text
    with pytest.raises(ValueError, match="invalid input name"):
        with_inputs("release", {"lower": "x"})


def test_quoted_inputs_reach_bash_unchanged() -> None:
    value = "a b'$(id)`x`\\n"
    done = subprocess.run(
        ["bash", "-c", with_inputs("release", {"VALUE": value}).split("\n", 1)[0] + '\nprintf %s "$VALUE"'],
        capture_output=True,
        text=True,
        check=True,
    )

    assert done.stdout == value


@pytest.mark.parametrize("report", ["", "  \n"])
def test_no_report_means_no_job_yet(report: str) -> None:
    assert parse_run_id(report) is None


@pytest.mark.parametrize(
    "report",
    [
        "not json",
        "[]",
        '{"run_id": "5"}',
        '{"run_id": 0}',
        '{"run_id": true}',
        '{"run_id": 9007199254740993}',
    ],
)
def test_a_hostile_report_is_refused(report: str) -> None:
    with pytest.raises(GuestError):
        parse_run_id(report)


@pytest.mark.parametrize(
    "report", ["[" * 100_000, "{" + '"run_id": 1,' * 50 + '"x": 1}', '{"run_id": 1}' + " " * 300 + "x"]
)
def test_an_oversized_or_deeply_nested_report_is_a_guest_error(report: str) -> None:
    with pytest.raises(GuestError):
        parse_run_id(report)


def test_the_probe_tries_every_host_address_not_only_the_gateway() -> None:
    import socket

    listener = socket.socket()
    for port in (8006, 3128):
        try:
            listener.bind(("127.0.0.1", port))
            break
        except OSError:
            continue
    else:
        pytest.skip("neither probe port is free on this host")
    listener.listen()
    try:
        probe = with_inputs("probe", {"GATEWAY": "192.0.2.1", "HOST_ADDRESSES": "198.51.100.7 127.0.0.1"})
        done = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, timeout=30, check=False)
    finally:
        listener.close()

    assert done.returncode == 3
    assert "answered on 127.0.0.1" in done.stderr


def test_a_valid_report_names_its_run() -> None:
    assert parse_run_id('{"run_id": 42}\n') == 42


def test_every_guest_script_is_strict_bash_and_part_of_the_payload_version() -> None:
    for name in ("prepare", "probe", "start", "report", "finished", "attach", "release", "tidy", "logs"):
        assert script(f"{name}.sh").startswith("#!/bin/bash\n")
    assert len(payload_version()) == 16
