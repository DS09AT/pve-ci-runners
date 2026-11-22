import json
import os
import time
from pathlib import Path

import pytest

from pve_ci_runners.cache import NoCache
from pve_ci_runners.cache_zfs import KEY_PROPERTY, PROMOTING_PROPERTY, RUN_PROPERTY, RUNNER_PROPERTY, ZfsCache
from pve_ci_runners.config import Config
from pve_ci_runners.drain import drain, draining, reserve, reserved_mb, resume
from pve_ci_runners.errors import CommandError, GuestError
from pve_ci_runners.logs import prune_logs
from pve_ci_runners.proc import HostCommands, output, succeeds
from pve_ci_runners.pve import Pve
from pve_ci_runners.state import CacheLease
from pve_ci_runners.templates import TemplateMarker, current_template, decode_marker
from tests.fakes import Script, done


def test_host_commands_report_exit_status_timeouts_and_missing_tools() -> None:
    commands = HostCommands()
    assert output(commands, ["sh", "-c", "printf ok"]) == "ok"
    assert not succeeds(commands, ["sh", "-c", "exit 3"])
    with pytest.raises(CommandError, match="exited 3: boom"):
        output(commands, ["sh", "-c", "echo boom >&2; exit 3"])
    with pytest.raises(CommandError, match="timed out"):
        commands.run(["sleep", "5"], timeout=0.1)
    with pytest.raises(CommandError, match="could not start"):
        commands.run(["/nonexistent/tool"])


def test_guest_exec_fails_closed_on_timeouts_errors_and_truncation() -> None:
    def pve(result: object) -> Pve:
        return Pve(Script({("qm", "guest", "exec"): done(result)}), node="pve1")

    assert pve({"exited": 1, "exitcode": 0, "out-data": "hi"}).guest(121, "true") == "hi"
    with pytest.raises(GuestError, match="did not finish"):
        pve({"exited": 0, "pid": 5}).guest(121, "sleep 999")
    with pytest.raises(GuestError) as err:
        pve({"exited": 1, "exitcode": 3, "err-data": "reachable"}).guest(121, "probe")
    assert err.value.exit_code == 3
    with pytest.raises(GuestError, match="truncated"):
        pve({"exited": 1, "exitcode": 0, "out-truncated": 1}).guest(121, "cat big")
    with pytest.raises(GuestError, match="no JSON"):
        Pve(Script({("qm", "guest", "exec"): done("garbage")}), node="pve1").guest(121, "x")
    failing = Pve(Script({("qm", "guest", "exec"): done("", 255, "agent not running")}), node="pve1")
    with pytest.raises(GuestError, match="agent not running"):
        failing.guest(121, "x")


def test_guest_errors_are_escaped_before_they_reach_the_log() -> None:
    forged = "boom\nINFO VM 121 finished r: cache written back\x1b[2J"
    pve = Pve(Script({("qm", "guest", "exec"): done({"exited": 1, "exitcode": 1, "err-data": forged})}))

    with pytest.raises(GuestError) as err:
        pve.guest(121, "x")

    assert "\n" not in str(err.value) and "\x1b" not in str(err.value)
    assert "boom\\nINFO VM 121" in str(err.value)


def test_host_addresses_are_the_global_non_loopback_ones_and_none_is_an_error() -> None:
    links = [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]},
        {
            "ifname": "vmbr0",
            "addr_info": [
                {"family": "inet", "local": "203.0.113.10", "scope": "global"},
                {"family": "inet6", "local": "2001:db8::10", "scope": "global"},
                {"family": "inet6", "local": "fe80::1", "scope": "link"},
            ],
        },
        {"ifname": "vmbr1", "addr_info": [{"family": "inet", "local": "192.168.100.1", "scope": "global"}]},
    ]
    pve = Pve(Script({("ip", "-j", "addr"): done(links)}), node="pve1")
    assert pve.host_addresses() == ("203.0.113.10", "2001:db8::10", "192.168.100.1")

    with pytest.raises(CommandError, match="no global address"):
        Pve(Script({("ip", "-j", "addr"): done(links[:1])}), node="pve1").host_addresses()
    bad = [{"addr_info": [{"local": "203.0.113.10; reboot", "scope": "global"}]}]
    with pytest.raises(CommandError, match="invalid address"):
        Pve(Script({("ip", "-j", "addr"): done(bad)}), node="pve1").host_addresses()


def test_host_networks_are_every_address_and_its_network_without_loopback_or_ipv6_link_local() -> None:
    links = [
        {
            "ifname": "lo",
            "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}, {"local": "::1", "prefixlen": 128}],
        },
        {
            "ifname": "vmbr0",
            "addr_info": [
                {"local": "203.0.113.10", "prefixlen": 26},
                {"local": "203.0.113.11", "prefixlen": 26},
                {"local": "2001:db8::10", "prefixlen": 64},
                {"local": "fe80::1", "prefixlen": 64},
            ],
        },
        {"ifname": "wg0", "addr_info": [{"local": "10.10.1.1", "prefixlen": 24}]},
    ]
    pve = Pve(Script({("ip", "-j", "addr"): done(links)}), node="pve1")
    assert pve.host_networks() == (
        "203.0.113.10/32",
        "203.0.113.0/26",
        "203.0.113.11/32",
        "2001:db8::10/128",
        "2001:db8::/64",
        "10.10.1.1/32",
        "10.10.1.0/24",
    )

    with pytest.raises(CommandError, match="no address"):
        Pve(Script({("ip", "-j", "addr"): done(links[:1])}), node="pve1").host_networks()
    for bad in ({"local": "203.0.113.10; reboot", "prefixlen": 24}, {"local": "203.0.113.10"}):
        with pytest.raises(CommandError, match="invalid address"):
            Pve(Script({("ip", "-j", "addr"): done([{"addr_info": [bad]}])}), node="pve1").host_networks()


def test_guest_exec_passes_stdin_and_the_script_as_separate_arguments() -> None:
    script = Script({("qm", "guest", "exec"): done({"exited": 1, "exitcode": 0})})
    Pve(script, node="pve1").guest(121, "echo $X; rm -rf /", stdin="secret", timeout=30)

    assert script.calls[0] == [
        "qm",
        "guest",
        "exec",
        "121",
        "--timeout",
        "30",
        "--pass-stdin",
        "1",
        "--",
        "bash",
        "-c",
        "echo $X; rm -rf /",
    ]
    assert script.stdin[0] == "secret"


def test_node_listing_memory_and_options(tmp_path: Path) -> None:
    gib = 2**30
    script = Script({
        ("pvesh", "get", "/nodes/pve1/qemu"): done([
            {"vmid": 101, "name": "db", "status": "running", "maxmem": 8 * gib, "tags": "prod"},
            {"vmid": 9201, "name": "tpl", "status": "stopped", "maxmem": 3 * gib, "template": 1},
            {"vmid": "bad"},
        ]),
        ("pvesh", "get", "/nodes/pve1/lxc"): done([{"status": "running", "maxmem": 2 * gib}]),
        ("pvesh", "get", "/nodes/pve1/status"): done({"memory": {"total": 64 * gib}}),
        ("pvesh", "get", "/nodes/pve1/qemu/121/config"): done({
            "scsi0": "local-zfs:vm-121-disk-0,iothread=1,mbps_wr=5"
        }),
    })
    pve = Pve(script, node="pve1", firewall_dir=tmp_path)

    vms = pve.vms()
    assert set(vms) == {101, 9201}
    assert vms[101].running and vms[101].tags == frozenset({"prod"}) and vms[9201].template
    memory = pve.host_memory({})
    assert (memory.total_mb, memory.committed_mb) == (65536, 10240)

    pve.limit_disk(121, "scsi0", [("mbps_wr", 200), ("iops_rd", 500)])
    assert script.calls[-1] == [
        "qm",
        "set",
        "121",
        "--scsi0",
        "local-zfs:vm-121-disk-0,iothread=1,mbps_wr=200,iops_rd=500",
    ]
    pve.write_firewall(121, "[OPTIONS]\n")
    assert (tmp_path / "121.fw").read_text(encoding="utf-8") == "[OPTIONS]\n"
    pve.configure(121, [("cores", "4"), ("description", "--delete scsi0")])
    assert script.calls[-1] == ["qm", "set", "121", "--cores", "4", "--description", "--delete scsi0"]


def test_host_memory_counts_stopped_guests_and_slot_vms_but_not_templates_or_build_vms() -> None:
    gib = 2**30
    build = {"vmid": 9202, "name": "ci-runner-template-9202", "tags": "pve-ci-runners", "maxmem": 5 * gib}
    pve = Pve(
        Script({
            ("pvesh", "get", "/nodes/pve1/qemu"): done([
                {"vmid": 101, "status": "stopped", "maxmem": 16 * gib},
                {"vmid": 121, "status": "stopped", "maxmem": 4 * gib},
                {"vmid": 9201, "status": "stopped", "maxmem": 3 * gib, "template": 1},
                {**build, "status": "running"},
            ]),
            ("pvesh", "get", "/nodes/pve1/lxc"): done([
                {"vmid": 200, "status": "stopped", "maxmem": 2 * gib}
            ]),
            ("pvesh", "get", "/nodes/pve1/status"): done({"memory": {"total": 64 * gib}}),
        }),
        node="pve1",
    )

    build_vms = {9201: "ci-runner-template-9201", 9202: "ci-runner-template-9202"}
    assert pve.host_memory(build_vms).committed_mb == (16 + 4 + 2) * 1024
    assert pve.host_memory({}).committed_mb == (16 + 4 + 2 + 5) * 1024


@pytest.mark.parametrize(
    ("kind", "guest"),
    [
        ("qemu", {"name": "ci-runner-template-9202"}),
        ("qemu", {"name": "ci-runner-template-9202", "tags": "prod"}),
        ("qemu", {"name": "windows", "tags": "pve-ci-runners"}),
        ("lxc", {"name": "ci-runner-template-9202", "tags": "pve-ci-runners"}),
    ],
)
def test_a_foreign_guest_or_a_container_on_a_build_vmid_is_counted(kind: str, guest: dict[str, str]) -> None:
    gib = 2**30
    listing = [{"vmid": 9202, "status": "stopped", "maxmem": 5 * gib, **guest}]
    pve = Pve(
        Script({
            ("pvesh", "get", "/nodes/pve1/qemu"): done(listing if kind == "qemu" else []),
            ("pvesh", "get", "/nodes/pve1/lxc"): done(listing if kind == "lxc" else []),
            ("pvesh", "get", "/nodes/pve1/status"): done({"memory": {"total": 64 * gib}}),
        }),
        node="pve1",
    )

    assert pve.host_memory({9202: "ci-runner-template-9202"}).committed_mb == 5 * 1024


def test_node_status_without_memory_is_an_error() -> None:
    pve = Pve(Script({("pvesh", "get", "/nodes/pve1/status"): done({})}), node="pve1")
    with pytest.raises(CommandError, match="memory total"):
        pve.host_memory({})
    with pytest.raises(CommandError, match="no JSON"):
        Pve(Script({("pvesh",): done("<html>")}), node="pve1").vms()


def test_vm_operations_call_qm_with_fixed_arguments(tmp_path: Path) -> None:
    script = Script({("qm", "agent"): done("", 2)})
    (tmp_path / "121.pid").write_text("4242\n", encoding="utf-8")
    (tmp_path / "4242").mkdir()
    pve = Pve(script, node="pve1", firewall_dir=tmp_path, run_dir=tmp_path, proc_dir=tmp_path)
    (tmp_path / "121.fw").write_text("[OPTIONS]\n", encoding="utf-8")
    (tmp_path / "122.fw").write_text("[OPTIONS]\n", encoding="utf-8")
    pve.clone(9201, 121, "ci-runner-121")
    pve.start(121)
    pve.attach(121, "scsi1", "/dev/zvol/rpool/x", "cicache", limits=[("mbps_wr", 200), ("iops_rd", 500)])
    pve.detach(121, "scsi1")
    pve.set_description(121, "{}")
    pve.destroy(121)
    pve.destroy(121)
    assert not pve.agent_ready(121)
    assert script.calls[:4] == [
        ["qm", "clone", "9201", "121", "--name", "ci-runner-121", "--full", "0"],
        ["qm", "start", "121"],
        [
            "qm",
            "set",
            "121",
            "--scsi1",
            "/dev/zvol/rpool/x,serial=cicache,discard=on,mbps_wr=200,iops_rd=500",
        ],
        ["qm", "set", "121", "--delete", "scsi1"],
    ]
    assert not (tmp_path / "121.fw").exists(), "a destroyed VM leaves no firewall file behind"
    assert (tmp_path / "122.fw").exists()


def test_template_build_operations_call_qm_and_pvesm_with_fixed_arguments() -> None:
    script = Script({
        ("pvesm", "path", "local:snippets/a.yaml"): done("/var/lib/vz/snippets/a.yaml\n"),
        ("pvesh", "get", "/cluster/firewall/options"): done({"enable": 1}),
    })
    pve = Pve(script, node="pve1")

    pve.create(9201, [("name", "tpl"), ("memory", "4096")])
    pve.resize(9201, "scsi0", "40G")
    pve.shutdown(9201, 120)
    pve.make_template(9201)
    assert pve.snippet_path("local:snippets/a.yaml") == Path("/var/lib/vz/snippets/a.yaml")
    assert pve.firewall_enabled()
    assert script.calls[:4] == [
        ["qm", "create", "9201", "--name", "tpl", "--memory", "4096"],
        ["qm", "disk", "resize", "9201", "scsi0", "40G"],
        ["qm", "shutdown", "9201", "--timeout", "120"],
        ["qm", "template", "9201"],
    ]


def test_unexpected_node_answers_are_errors() -> None:
    pve = Pve(
        Script({
            ("pvesm", "path"): done("relative/a.yaml\n"),
            ("pvesh", "get", "/nodes/pve1/qemu/121/config"): done(["not", "a", "table"]),
            ("pvesh", "get", "/nodes/pve1/qemu/122/config"): done({"scsi0": "local-zfs:vm-122-disk-0"}),
            ("pvesh", "get", "/nodes/pve1/qemu"): done({"not": "a list"}),
            ("qm", "guest", "exec"): done(["not", "an", "object"]),
        }),
        node="pve1",
    )

    with pytest.raises(CommandError, match="no absolute path"):
        pve.snippet_path("local:snippets/a.yaml")
    with pytest.raises(CommandError, match="returned no configuration"):
        pve.config(121)
    with pytest.raises(CommandError, match="has no disk scsi1"):
        pve.limit_disk(122, "scsi1", [("mbps_wr", 200)])
    with pytest.raises(GuestError, match="no JSON object"):
        pve.guest(121, "true")
    assert pve.vms() == {}


def test_a_started_vm_is_the_first_the_kernel_sacrifices_or_it_is_stopped_again(tmp_path: Path) -> None:
    script = Script()
    (tmp_path / "121.pid").write_text("4242\n", encoding="utf-8")
    (tmp_path / "4242").mkdir()
    pve = Pve(script, node="pve1", run_dir=tmp_path, proc_dir=tmp_path)

    pve.start(121)
    assert (tmp_path / "4242" / "oom_score_adj").read_text(encoding="utf-8") == "1000"

    with pytest.raises(CommandError, match="out-of-memory priority"):
        pve.start(122)
    assert ["qm", "stop", "122", "--skiplock", "1"] in script.calls


class _TimedOutStart(Script):
    def run(self, argv, *, stdin=None, timeout=300):
        if list(argv[:2]) == ["qm", "start"]:
            self.calls.append(list(argv))
            raise CommandError("qm start timed out after 180 s")
        return super().run(argv, stdin=stdin, timeout=timeout)


@pytest.mark.parametrize("script", [Script({("qm", "start"): done("", 1)}), _TimedOutStart()])
def test_a_vm_whose_start_fails_is_stopped_because_qemu_may_run_anyway(
    tmp_path: Path, script: Script
) -> None:
    pve = Pve(script, node="pve1", run_dir=tmp_path, proc_dir=tmp_path)

    with pytest.raises(CommandError, match="failed to start and was stopped"):
        pve.start(121)
    assert script.calls[-1] == ["qm", "stop", "121", "--skiplock", "1"]


def test_a_vm_that_fails_to_start_and_to_stop_is_reported_as_possibly_unprotected(tmp_path: Path) -> None:
    pve = Pve(Script({("qm", "stop"): done("", 1)}), node="pve1", run_dir=tmp_path, proc_dir=tmp_path)

    with pytest.raises(CommandError, match="may run unprotected"):
        pve.start(121)


def test_protecting_a_vm_sets_and_reads_back_its_out_of_memory_priority(tmp_path: Path) -> None:
    pve = Pve(Script(), node="pve1", run_dir=tmp_path, proc_dir=tmp_path)
    (tmp_path / "121.pid").write_text("4242\n", encoding="utf-8")
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "oom_score_adj").write_text("0\n", encoding="utf-8")

    pve.protect(121)
    assert (tmp_path / "4242" / "oom_score_adj").read_text(encoding="utf-8") == "1000"

    (tmp_path / "121.pid").write_text("not a pid\n", encoding="utf-8")
    with pytest.raises(CommandError, match="out-of-memory priority"):
        pve.protect(121)


def zfs_cache(config: Config, script: Script, clock: float = 2_000_000_000.0) -> ZfsCache:
    return ZfsCache(
        config.cache,
        script,
        clock=lambda: clock,
        sleep=lambda _: None,
        device_exists=lambda _: True,
    )


ROOM = {("zfs", "get", "-Hp", "-o", "value", "available", "rpool/ci-cache"): done(f"{21 * 2**30}\n")}
RUNNER = "ci-runner-121-abc123"


def test_a_first_lease_creates_an_empty_formatted_cache(config: Config) -> None:
    script = Script({**ROOM, ("zfs", "list", "-H", "-t", "snapshot"): done("", 1)})

    attachment = zfs_cache(config, script).lease("o__r/ci__build", 121, runner=RUNNER, run_id=42)

    flat = [" ".join(c) for c in script.calls]
    assert flat[0] == "zfs create -p rpool/ci-cache/jobs"
    assert any(
        c.startswith("zfs create -p -s -V 20G -o volblocksize=16k rpool/ci-cache/repos/o__r/ci__build")
        for c in flat
    )
    assert any(c.startswith("mkfs.ext4 -q -L ci-cache") for c in flat)
    identity = f"-o {KEY_PROPERTY}=o__r/ci__build -o {RUNNER_PROPERTY}={RUNNER} -o {RUN_PROPERTY}=42"
    assert f"zfs clone {identity}" in " ".join(flat)
    assert attachment is not None
    assert attachment.device == "/dev/zvol/rpool/ci-cache/jobs/o__r.ci__build-121-2000000000"


def test_a_pool_without_room_for_one_more_volume_gives_no_cache(config: Config) -> None:
    script = Script({("zfs", "get", "-Hp", "-o", "value", "available"): done(f"{20 * 2**30 - 1}\n")})

    assert zfs_cache(config, script).lease("o__r/ci__build", 121, runner=RUNNER, run_id=42) is None
    assert not [c for c in script.calls if c[:2] in (["zfs", "clone"], ["zfs", "snapshot"])]


CLONE = "rpool/ci-cache/jobs/o__r.ci__build-121-5"
IDENTITY = ("zfs", "get", "-H", "-o", "value", f"{KEY_PROPERTY},{RUNNER_PROPERTY},{RUN_PROPERTY}", CLONE)
OWNED = {IDENTITY: done(f"o__r/ci__build\n{RUNNER}\n42\n")}


def test_write_back_promotes_only_when_no_other_job_wrote_first(config: Config) -> None:
    lease = CacheLease("o__r/ci__build", CLONE, "rpool/ci-cache/repos/o__r/ci__build@g1")
    newer = Script({
        **OWNED,
        ("zfs", "list", "-H", "-t", "snapshot"): done("rpool/ci-cache/repos/o__r/ci__build@g2\n"),
    })
    assert not zfs_cache(config, newer).write_back(lease, 121, runner=RUNNER, run_id=42)

    same = Script({
        **OWNED,
        ("zfs", "list", "-H", "-t", "snapshot"): done("rpool/ci-cache/repos/o__r/ci__build@g1\n"),
    })
    assert zfs_cache(config, same).write_back(lease, 121, runner=RUNNER, run_id=42)
    steps = [" ".join(c[:2]) for c in same.calls if c[0] == "zfs" and c[1] not in {"list", "get"}]
    assert steps[:5] == ["zfs set", "zfs snapshot", "zfs promote", "zfs destroy", "zfs rename"]
    for name in (KEY_PROPERTY, RUNNER_PROPERTY, RUN_PROPERTY):
        assert ["zfs", "inherit", name, "rpool/ci-cache/repos/o__r/ci__build"] in same.calls


def test_a_write_back_in_the_origins_second_snapshots_above_the_origin(config: Config) -> None:
    lease = CacheLease("o__r/ci__build", CLONE, "rpool/ci-cache/repos/o__r/ci__build@g2000000000")
    script = Script({**OWNED, ("zfs", "list", "-H", "-t", "snapshot"): done(f"{lease.origin}\n")})

    assert zfs_cache(config, script, clock=2_000_000_000.0).write_back(lease, 121, runner=RUNNER, run_id=42)
    assert ["zfs", "snapshot", f"{CLONE}@g2000000001"] in script.calls


@pytest.mark.parametrize(("runner", "run_id"), [("ci-runner-121-def456", 42), (RUNNER, 41)])
def test_a_clone_is_only_written_back_for_the_runner_and_run_it_was_leased_for(
    config: Config, runner: str, run_id: int
) -> None:
    script = Script({
        **OWNED,
        ("zfs", "list", "-H", "-t", "snapshot"): done("rpool/ci-cache/repos/o__r/ci__build@g1\n"),
    })
    lease = CacheLease("o__r/ci__build", CLONE, "rpool/ci-cache/repos/o__r/ci__build@g1")

    assert not zfs_cache(config, script).write_back(lease, 121, runner=runner, run_id=run_id)
    assert [c for c in script.calls if c[1] not in {"list", "get"}] == []


@pytest.mark.parametrize(
    ("clone", "origin", "vmid"),
    [
        ("tank/other/vm-101-disk-0", "rpool/ci-cache/repos/o__r/ci__build@g1", 121),
        (CLONE, "tank/other@snap", 121),
        (CLONE, "rpool/ci-cache/repos/o__r/ci__build@g1", 122),
        ("rpool/ci-cache/jobs/o__r.ci__test-121-5", "rpool/ci-cache/repos/o__r/ci__build@g1", 121),
    ],
)
def test_a_lease_this_backend_did_not_create_for_the_vm_is_never_touched(
    config: Config, clone: str, origin: str, vmid: int
) -> None:
    script = Script({
        **OWNED,
        ("zfs", "list", "-H", "-t", "snapshot"): done(f"{origin}\n"),
        ("zfs", "get", "-Hp"): done("99999999\n"),
    })
    cache = zfs_cache(config, script)
    lease = CacheLease("o__r/ci__build", clone, origin)

    assert cache.written_bytes(lease, vmid) == 0
    assert not cache.write_back(lease, vmid, runner=RUNNER, run_id=42)
    cache.drop(lease, vmid)

    assert [c for c in script.calls if c[1] not in {"list", "get"}] == []


def test_a_clone_whose_root_only_key_property_differs_is_never_touched(config: Config) -> None:
    script = Script({IDENTITY: done(f"o__x/ci__build\n{RUNNER}\n42\n")})
    lease = CacheLease("o__r/ci__build", CLONE, "rpool/ci-cache/repos/o__r/ci__build@g1")

    zfs_cache(config, script).drop(lease, 121)

    assert ["zfs", "destroy", "-r", CLONE] not in script.calls
    owned = Script(OWNED)
    zfs_cache(config, owned).drop(lease, 121)
    assert ["zfs", "destroy", "-r", CLONE] in owned.calls


def test_maintenance_finishes_an_interrupted_promotion_and_drops_a_half_done_one(config: Config) -> None:
    jobs = (
        "rpool/ci-cache/jobs/promoted\t1\tk__a/b\n"
        "rpool/ci-cache/jobs/started\t1\tk__a/c\n"
        "rpool/ci-cache/jobs/busy\t-\tk__a/d\n"
    )
    script = Script({
        ("zfs", "list", "-H", "-o", f"name,{PROMOTING_PROPERTY},{KEY_PROPERTY}"): done(jobs),
        ("zfs", "get", "-H", "-o", "value", "origin", "rpool/ci-cache/jobs/promoted"): done("-\n"),
        ("zfs", "get", "-H", "-o", "value", "origin", "rpool/ci-cache/jobs/started"): done(
            "rpool/ci-cache/repos/k__a/c@g1\n"
        ),
        ("zpool", "list"): done("40%\n"),
    })

    lines = zfs_cache(config, script).maintain({"rpool/ci-cache/jobs/busy"})

    assert "finished the interrupted write-back of the cache k__a/b" in lines
    assert "dropped the half-promoted clone rpool/ci-cache/jobs/started" in lines
    assert ["zfs", "rename", "rpool/ci-cache/jobs/promoted", "rpool/ci-cache/repos/k__a/b"] in script.calls
    assert ["zfs", "destroy", "-r", "rpool/ci-cache/jobs/busy"] not in script.calls


def test_maintenance_expires_old_caches_and_frees_a_full_pool(config: Config) -> None:
    now = 2_000_000_000
    old, recent = now - 40 * 86400, now - 86400
    script = Script({
        ("zfs", "list", "-H", "-o", f"name,{PROMOTING_PROPERTY},{KEY_PROPERTY}"): done(""),
        ("zfs", "list", "-H", "-o", "name", "-r"): done(
            "rpool/ci-cache/repos/a/old\nrpool/ci-cache/repos/a/new\n"
        ),
        (
            "zfs",
            "list",
            "-H",
            "-t",
            "snapshot",
            "-o",
            "name",
            "-s",
            "creation",
            "-d",
            "1",
            "rpool/ci-cache/repos/a/old",
        ): done(f"rpool/ci-cache/repos/a/old@g{old}\n"),
        (
            "zfs",
            "list",
            "-H",
            "-t",
            "snapshot",
            "-o",
            "name",
            "-s",
            "creation",
            "-d",
            "1",
            "rpool/ci-cache/repos/a/new",
        ): done(f"rpool/ci-cache/repos/a/new@g{recent}\n"),
        ("zpool", "list"): [done("90%\n"), done("70%\n")],
    })

    lines = zfs_cache(config, script, clock=float(now)).maintain(set())

    assert any("expired the cache rpool/ci-cache/repos/a/old" in line for line in lines)
    assert any("pool at 90%" in line for line in lines)


def test_a_device_that_never_appears_is_an_error(config: Config) -> None:
    ticks = iter(range(100))
    cache = ZfsCache(
        config.cache,
        Script({**ROOM, ("zfs", "list", "-H", "-t", "snapshot"): done("x@g1\n")}),
        clock=lambda: float(next(ticks)),
        sleep=lambda _: None,
        device_exists=lambda _: False,
    )
    with pytest.raises(CommandError, match="did not appear"):
        cache.lease("o__r/ci__build", 121, runner=RUNNER, run_id=42)


def test_a_purge_destroys_the_clones_then_the_caches_and_skips_what_is_gone(config: Config) -> None:
    script = Script({("zfs", "list", "-H", "-o", "name", "rpool/ci-cache/jobs"): done("", 1)})

    zfs_cache(config, script).purge()

    assert [" ".join(c) for c in script.calls] == [
        "zfs list -H -o name rpool/ci-cache/jobs",
        "zfs list -H -o name rpool/ci-cache/repos",
        "zfs destroy -r rpool/ci-cache/repos",
    ]
    script = Script()
    zfs_cache(config, script).purge()
    assert [" ".join(c) for c in script.calls if c[1] == "destroy"] == [
        "zfs destroy -r rpool/ci-cache/jobs",
        "zfs destroy -r rpool/ci-cache/repos",
    ]
    NoCache().purge()


def test_drain_flag_round_trip(tmp_path: Path) -> None:
    assert not draining(tmp_path)
    drain(tmp_path / "state")
    assert draining(tmp_path / "state")
    resume(tmp_path / "state")
    resume(tmp_path / "state")
    assert not draining(tmp_path / "state")


def test_a_reservation_is_replaced_whole_and_an_empty_one_keeps_everything_reserved(tmp_path: Path) -> None:
    reservation = tmp_path / "reservation"
    reservation.write_text("", encoding="utf-8")
    assert reserved_mb(tmp_path) == 2**31
    before = reservation.stat().st_ino

    reserve(tmp_path, 4096)

    assert reserved_mb(tmp_path) == 4096
    assert reservation.stat().st_ino != before, "written beside it and renamed, never truncated in place"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["reservation"]


def test_old_logs_are_pruned_and_symlinks_left_alone(tmp_path: Path) -> None:
    old, new, link = tmp_path / "old.log", tmp_path / "new.log", tmp_path / "link.log"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    link.symlink_to(old)
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))

    assert prune_logs(tmp_path, 14, time.time()) == 1
    assert not old.exists() and new.exists() and link.is_symlink()
    assert prune_logs(tmp_path / "missing", 14, time.time()) == 0


def test_the_newest_finished_template_wins() -> None:
    from pve_ci_runners.pve import VmInfo

    vms = {v: VmInfo(v, "t", False, 1, True, frozenset()) for v in (9201, 9202)}
    marks = {9201: TemplateMarker(100, "2.338.2").encode(), 9202: TemplateMarker(200, "2.339.0").encode()}
    assert current_template((9201, 9202), vms, marks.__getitem__) == (9202, TemplateMarker(200, "2.339.0"))
    marks[9202] = "interrupted"
    assert current_template((9201, 9202), vms, marks.__getitem__)[0] == 9201  # type: ignore[index]
    assert current_template((9201, 9202), {}, marks.__getitem__) is None
    for text in ("", "[]", json.dumps({"kind": "other", "built_at": 1, "runner_version": "x"})):
        assert decode_marker(text) is None
