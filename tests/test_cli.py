import json
from pathlib import Path

import pytest

from pve_ci_runners import cli
from pve_ci_runners.config import parse
from pve_ci_runners.doctor import Finding
from pve_ci_runners.drain import draining
from pve_ci_runners.errors import CommandError
from pve_ci_runners.state import VmState
from pve_ci_runners.status import collect
from tests.conftest import config_data
from tests.fakes import FakeHost, FakeVm

CONFIG = """
[github]
owner = "example-org"
app_client_id = "Iv23liEXAMPLE"
[capacity]
memory_mb = 8192
[network]
bridge = "vmbr1"
gateway = "192.168.100.1"
prefix = 24
dns = ["192.0.2.53"]
[[slot]]
vmid = 121
address = "192.168.100.21"
[[profile]]
name = "default"
match = "pve-ci"
labels = ["self-hosted", "pve-ci"]
cores = 2
memory_mb = 4096
max = 1
[cache]
backend = "none"
[template]
vmids = [9201, 9202]
storage = "local-zfs"
image_url = "https://images.example.org/noble-server-cloudimg-amd64.img"
image_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
address = "192.168.100.20"
[daemon]
state_dir = "{state}"
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.replace("{state}", str(tmp_path / "state")), encoding="utf-8")
    return path


def test_config_check_drain_resume_and_version(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["-c", str(config_file), "config", "check"]) == 0
    assert "valid, 1 slots, 1 profiles" in capsys.readouterr().out
    assert cli.main(["-c", str(config_file), "drain"]) == 0
    assert draining(config_file.parent / "state")
    assert cli.main(["-c", str(config_file), "resume"]) == 0
    assert not draining(config_file.parent / "state")
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip().endswith(cli.VERSION)


def test_errors_are_one_line_and_a_non_zero_exit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["-c", str(tmp_path / "missing.toml"), "config", "check"]) == 1
    assert capsys.readouterr().err.startswith("pve-ci-runners: ")


def test_an_unexpected_error_between_polls_does_not_stop_the_controller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Failing:
        def attach_pending(self) -> None:
            raise RecursionError("deeply nested guest report")

    cli._attach(Failing())  # type: ignore[arg-type]

    assert "attach failed" in caplog.text


def test_status_reads_every_slot_from_the_host(tmp_path: Path) -> None:
    config = parse(config_data(daemon={"state_dir": str(tmp_path)}))
    host = FakeHost()
    host.add_template(9201)
    state = VmState("assigned", 1000, "default", "0123456789abcdef", repository="example-org/app")
    host.machines[121] = FakeVm(
        name="ci-runner-121", running=True, tags={"pve-ci-runners"}, description=state.encode()
    )

    status = collect(config, host)

    data = json.loads(status.to_json())
    assert data["template"] == 9201
    assert [s["phase"] for s in data["slots"]] == ["assigned", "free"]
    assert "example-org/app" in status.to_text()
    host.machines[122] = FakeVm(name="other")
    assert collect(config, host).slots[1].phase == "foreign"


def test_doctor_prints_every_finding_and_fails_on_a_failed_check(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = [
        Finding("ok", "bridge", "vmbr1 is up"),
        Finding("warn", "NAT", "no masquerade", "nft add rule"),
    ]

    class FakeDoctor:
        def __init__(self, *_args: object) -> None:
            pass

        def run(self) -> list[Finding]:
            return list(findings)

    monkeypatch.setattr(cli, "Doctor", FakeDoctor)
    assert cli.main(["-c", str(config_file), "doctor"]) == 0
    assert (
        capsys.readouterr().out
        == "OK    bridge: vmbr1 is up\nWARN  NAT: no masquerade\n      fix: nft add rule\n"
    )

    findings.append(Finding("fail", "template", "no finished template"))
    assert cli.main(["-c", str(config_file), "doctor"]) == 1

    capsys.readouterr()
    assert cli.main(["-c", str(config_file), "doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "1"
    assert report["findings"][2] == {
        "level": "fail",
        "check": "template",
        "detail": "no finished template",
        "fix": "",
    }


def test_teardown_passes_the_purge_flag_and_fails_on_any_failure(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    failures: list[str] = []

    class FakeTeardown:
        def __init__(self, *_args: object) -> None:
            pass

        def run(self, *, purge_cache: bool) -> list[str]:
            calls.append(purge_cache)
            return list(failures)

    monkeypatch.setattr(cli, "Teardown", FakeTeardown)
    assert cli.main(["-c", str(config_file), "teardown"]) == 0
    assert "torn down" in capsys.readouterr().out
    failures.append("VM 121: qm destroy 121 failed")
    assert cli.main(["-c", str(config_file), "teardown", "--purge-cache"]) == 1
    assert capsys.readouterr().err == "pve-ci-runners: VM 121: qm destroy 121 failed\n"
    assert calls == [False, True]


def with_app_key(config_file: Path, mode: int) -> None:
    key = config_file.parent / "app.pem"
    key.write_text("key", encoding="utf-8")
    key.chmod(mode)
    text = config_file.read_text(encoding="utf-8")
    config_file.write_text(
        text.replace("[capacity]", f'app_key_file = "{key}"\n[capacity]'), encoding="utf-8"
    )


def test_run_refuses_an_unprotected_app_key(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with_app_key(config_file, 0o644)

    assert cli.main(["-c", str(config_file), "run"]) == 1
    assert "too open" in capsys.readouterr().err


def test_run_attaches_caches_between_polls_until_interrupted(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_app_key(config_file, 0o600)
    now = [0.0]
    calls: list[str] = []

    def cycle(_controller: object) -> None:
        calls.append(f"cycle {now[0]:g}")
        if len([c for c in calls if c.startswith("cycle")]) == 2:
            raise KeyboardInterrupt

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(cli, "_cycle", cycle)
    monkeypatch.setattr(cli, "_attach", lambda _controller: calls.append(f"attach {now[0]:g}"))
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cli.time, "sleep", sleep)

    assert cli.main(["-c", str(config_file), "run"]) == 130
    assert calls == ["cycle 0", *(f"attach {t}" for t in range(0, 15, 2)), "cycle 16"]


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (
            CommandError("qm list exited 1"),
            ["cycle incomplete: qm list exited 1", "attach incomplete: qm list exited 1"],
        ),
        (KeyError("vmid"), ["cycle failed", "attach failed"]),
    ],
)
def test_a_failed_round_is_logged_and_does_not_stop_the_controller(
    caplog: pytest.LogCaptureFixture, error: Exception, logged: list[str]
) -> None:
    class Failing:
        def cycle(self) -> None:
            raise error

        def attach_pending(self) -> None:
            raise error

    cli._cycle(Failing())  # type: ignore[arg-type]
    cli._attach(Failing())  # type: ignore[arg-type]

    assert [r.getMessage().split("\n")[0] for r in caplog.records] == logged
