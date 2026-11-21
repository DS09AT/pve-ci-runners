from pathlib import Path

import pytest

from pve_ci_runners.config import Config, parse
from pve_ci_runners.doctor import Doctor, Finding
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
            github={"owner": "example-org", "app_client_id": "Iv23liEXAMPLE", "app_key_file": str(key)}
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
