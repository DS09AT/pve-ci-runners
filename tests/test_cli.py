import json
from pathlib import Path

import pytest

from pve_ci_runners import cli
from pve_ci_runners.config import parse
from pve_ci_runners.drain import draining
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
    assert capsys.readouterr().out.strip().endswith("0.1.0")


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
