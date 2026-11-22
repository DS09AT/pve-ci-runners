from pathlib import Path

import pytest

from pve_ci_runners.config import Config, parse
from pve_ci_runners.doctor import Doctor, Finding
from pve_ci_runners.drain import reserve
from pve_ci_runners.pve import TAG
from tests.conftest import config_data
from tests.fakes import FakeHost, FakeVm, Script, done

HEALTHY = {
    ("pvesh", "get", "/cluster/firewall/options"): done({"enable": 1}),
    ("ip", "-j", "addr", "show", "dev", "vmbr1"): done([
        {"addr_info": [{"local": "192.168.100.1", "prefixlen": 24}]}
    ]),
    ("sysctl", "-n", "net.ipv4.ip_forward"): done("1\n"),
    ("nft", "list", "ruleset"): done('\t\tip saddr 192.168.100.0/24 oifname "eno1" masquerade\n'),
    ("pvesh", "get", "/storage/local-zfs"): done({"type": "zfspool", "content": "images,rootdir"}),
    ("pvesh", "get", "/storage/local"): done({"type": "dir", "content": "iso,snippets,vztmpl"}),
    ("zfs", "list", "-H", "-o", "name", "rpool/ci-cache"): done("rpool/ci-cache\n"),
    ("pvesh", "get", "/cluster/backup"): done([]),
    ("cat", "/proc/spl/kstat/zfs/arcstats"): done(f"c_max                           4    {2 * 2**30}\n"),
}
ALL_GRANTED = {
    "administration": "write",
    "actions": "read",
    "metadata": "read",
    "contents": "read",
    "actions_variables": "write",
}


class App:
    def __init__(self, granted: dict[str, str], latest: str = "v2.338.2") -> None:
        self.granted = granted
        self.latest = latest

    def permissions(self) -> dict[str, str]:
        return dict(self.granted)

    def get(self, path: str, *, conditional: bool = False) -> object:
        assert path == "/repos/actions/runner/releases/latest"
        return {"tag_name": self.latest}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    key = tmp_path / "app.pem"
    key.write_text("key", encoding="utf-8")
    key.chmod(0o600)
    return parse(
        config_data(
            github={"owner": "example-org", "app_client_id": "Iv23liEXAMPLE", "app_key_file": str(key)},
            daemon={"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "log")},
        )
    )


def run(
    config: Config, answers: dict, host: FakeHost | None = None, app: App | None = None
) -> dict[str, Finding]:
    if host is None:
        host = FakeHost()
        host.add_template(9201)
        host.machines[9201].name = "ci-runner-template-9201"
        host.machines[9201].tags = {TAG}
    findings = Doctor(config, Script(answers), host, app or App(ALL_GRANTED)).run()
    return {f.check: f for f in findings}


def test_a_ready_node_passes_every_check(config: Config) -> None:
    findings = run(config, HEALTHY)

    assert {f.level for f in findings.values()} == {"ok"}, [f.to_text() for f in findings.values()]


@pytest.mark.parametrize(
    ("command", "answer", "check"),
    [
        (("pvesh", "get", "/cluster/firewall/options"), done({"enable": 0}), "datacenter firewall"),
        (("ip", "-j", "addr", "show", "dev", "vmbr1"), done([]), "bridge"),
        (("sysctl", "-n", "net.ipv4.ip_forward"), done("0\n"), "forwarding"),
        (("pvesh", "get", "/storage/local"), done({"type": "dir", "content": "iso"}), "snippet storage"),
        (
            ("pvesh", "get", "/storage/local-zfs"),
            done({"type": "zfspool", "content": "rootdir"}),
            "template storage",
        ),
        (("zfs", "list", "-H", "-o", "name", "rpool/ci-cache"), done("", 1), "cache dataset"),
        (
            ("pvesh", "get", "/cluster/backup"),
            done([{"id": "nightly", "all": 1, "exclude": "121"}]),
            "backup jobs",
        ),
        (("pvesh", "get", "/cluster/backup"), done([{"id": "vms", "vmid": "100,122"}]), "backup jobs"),
        (("pvesh", "get", "/cluster/firewall/options"), done("not json"), "datacenter firewall"),
        (("pvesh", "get", "/storage/local"), done("not json"), "snippet storage"),
        (("pvesh", "get", "/storage/local-zfs"), done(["not", "an", "object"]), "template storage"),
    ],
)
def test_each_problem_fails_its_check_with_a_fix(config: Config, command: tuple, answer, check: str) -> None:
    findings = run(config, {**HEALTHY, command: answer})

    assert findings[check].level == "fail"
    assert findings[check].fix or "could not be checked" in findings[check].detail


def test_a_disabled_or_fully_excluded_backup_job_is_fine(config: Config) -> None:
    jobs = [{"id": "off", "all": 1, "enabled": 0}, {"id": "rest", "all": 1, "exclude": "121,122,9201,9202"}]

    assert (
        run(config, {**HEALTHY, ("pvesh", "get", "/cluster/backup"): done(jobs)})["backup jobs"].level == "ok"
    )


def test_missing_nat_and_slow_storage_are_warnings(config: Config) -> None:
    findings = run(
        config,
        {
            **HEALTHY,
            ("nft", "list", "ruleset"): done("table inet filter {}\n"),
            ("pvesh", "get", "/storage/local-zfs"): done({"type": "dir", "content": "images"}),
        },
    )

    assert findings["outbound NAT"].level == "warn"
    assert findings["template storage"].level == "warn"


def test_vmids_held_by_foreign_vms_are_named(config: Config) -> None:
    host = FakeHost()
    host.machines[122] = FakeVm(name="database", tags=set())

    findings = run(config, HEALTHY, host)

    assert findings["VMIDs"].level == "fail" and "122" in findings["VMIDs"].detail
    assert (
        findings["template"].level == "fail" and findings["template"].fix == "pve-ci-runners template build"
    )


def test_missing_app_permissions_and_an_old_runner_are_reported(config: Config) -> None:
    granted = {"administration": "read", "actions": "read", "metadata": "read", "variables": "write"}

    findings = run(config, HEALTHY, app=App(granted, latest="v2.341.0"))

    assert findings["GitHub App"].level == "fail"
    assert "administration: write" in findings["GitHub App"].detail
    assert "contents: read" in findings["GitHub App"].detail
    assert "actions_variables: write" in findings["GitHub App"].detail
    assert findings["template"].level == "warn" and "2.341.0" in findings["template"].detail


def test_a_key_file_others_can_read_fails(config: Config) -> None:
    config.github.app_key_file.chmod(0o644)

    assert run(config, HEALTHY)["app key"].level == "fail"


@pytest.mark.parametrize(
    ("command", "answer", "check", "fix"),
    [
        (
            ("pvesh", "get", "/storage/local-zfs"),
            done({"type": "zfspool", "content": "rootdir,iso"}),
            "template storage",
            "pvesm set local-zfs --content rootdir,iso,images",
        ),
        (
            ("pvesh", "get", "/storage/local-zfs"),
            done({"type": "zfspool", "content": ""}),
            "template storage",
            "pvesm set local-zfs --content images",
        ),
        (
            ("pvesh", "get", "/storage/local"),
            done({"type": "dir"}),
            "snippet storage",
            "pvesm set local --content snippets",
        ),
        (
            ("pvesh", "get", "/cluster/backup"),
            done([{"id": "backup-nightly", "all": 1, "exclude": "121,500"}]),
            "backup jobs",
            "pvesh set /cluster/backup/backup-nightly --exclude 121,122,500,9201,9202",
        ),
        (
            ("pvesh", "get", "/cluster/backup"),
            done([{"id": "backup-vms", "vmid": "100,122,9201"}]),
            "backup jobs",
            "pvesh set /cluster/backup/backup-vms --vmid 100",
        ),
        (
            ("pvesh", "get", "/cluster/backup"),
            done([{"id": "backup-ci", "vmid": "121"}, {"id": "backup-all", "all": 1}]),
            "backup jobs",
            (
                "pvesh set /cluster/backup/backup-ci --enabled 0; "
                "pvesh set /cluster/backup/backup-all --exclude 121,122,9201,9202"
            ),
        ),
    ],
)
def test_a_fix_keeps_the_values_already_set(
    config: Config, command: tuple, answer, check: str, fix: str
) -> None:
    findings = run(config, {**HEALTHY, command: answer})

    assert findings[check].level == "fail" and findings[check].fix == fix


@pytest.mark.parametrize(("spare_mb", "level"), [(0, "ok"), (-1, "fail")])
def test_guest_memory_counts_every_other_guest_running_or_not(
    config: Config, spare_mb: int, level: str
) -> None:
    host = FakeHost(other_guests_mb=0)
    host.add_template(9201)
    host.machines[100] = FakeVm(name="windows", memory_mb=16000)
    host.machines[121] = FakeVm(name="ci-runner-121", memory_mb=5120, tags={TAG})
    host.machines[9202] = FakeVm(name="ci-runner-template-9202", memory_mb=4096, tags={TAG})
    reserve(config.daemon.state_dir, 4096)
    host.total_mb = 20480 + 4096 + 16000 + spare_mb

    finding = run(config, HEALTHY, host)["guest memory"]

    assert finding.level == level
    assert "capacity.memory_mb 20480 + capacity.host_reserve_mb 4096 + 16000 MiB" in finding.detail
    if level == "fail":
        assert "40575 MiB do not cover" in finding.detail and "= 40576 MiB" in finding.detail
        assert "by 1 MiB" in finding.fix and "stopped guests" in finding.fix


def test_guest_memory_counts_a_build_vm_without_a_reservation_and_foreign_vms_on_our_vmids(
    config: Config,
) -> None:
    host = FakeHost(other_guests_mb=0)
    host.add_template(9201)
    host.machines[121] = FakeVm(name="windows", memory_mb=5120)
    host.machines[9202] = FakeVm(name="ci-runner-template-9202", memory_mb=4096, tags={TAG})
    host.total_mb = 20480 + 4096 + 5120 + 4096

    finding = run(config, HEALTHY, host)["guest memory"]

    assert finding.level == "ok" and "+ 9216 MiB configured" in finding.detail


def test_a_reserve_that_does_not_cover_the_zfs_arc_is_a_warning(config: Config) -> None:
    arc = {("cat", "/proc/spl/kstat/zfs/arcstats"): done(f"c_max   4    {8 * 2**30}\n")}

    finding = run(config, {**HEALTHY, **arc})["memory reserve"]

    assert finding.level == "warn" and "9216" in finding.fix
    assert (
        run(config, {**HEALTHY, ("test", "-d", "/proc/spl/kstat/zfs"): done("", 1)})["memory reserve"].level
        == "ok"
    )


@pytest.mark.parametrize(
    "answer",
    [
        done("", 1, "cat: arcstats: Permission denied"),
        done(""),
        done("c_max   4\n"),
        done("c_max   4    abc\n"),
    ],
)
def test_arcstats_that_cannot_be_read_or_parsed_on_a_zfs_host_fail_the_check(config: Config, answer) -> None:
    finding = run(config, {**HEALTHY, ("cat", "/proc/spl/kstat/zfs/arcstats"): answer})["memory reserve"]

    assert finding.level == "fail" and finding.detail.startswith("could not be checked")
