import hashlib
import io
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from pve_ci_runners import images, template_build
from pve_ci_runners.config import Config, parse
from pve_ci_runners.drain import drain, draining, reserved_mb
from pve_ci_runners.errors import CommandError, PveCiError
from pve_ci_runners.images import fetch_image
from pve_ci_runners.lifecycle import IsolationError
from pve_ci_runners.pve import TAG
from pve_ci_runners.template_build import TemplateBuilder
from pve_ci_runners.templates import current_template, decode_marker
from tests.conftest import config_data
from tests.fakes import FakeHost, FakeVm, holds_memory_lock

IMAGE = b"cloud image bytes"


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Config, FakeHost]:
    config = parse(
        config_data(daemon={"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "log")})
    )
    host = FakeHost()
    host.snippet_dir = tmp_path
    return config, host


def builder(config: Config, host: FakeHost, now: float = 5000.0, root: Path = Path("/")) -> TemplateBuilder:
    return TemplateBuilder(
        config,
        host,
        clock=lambda: now,
        sleep=lambda _: None,
        fetch=lambda *_: Path("/images/noble.img"),
        script_root=root,
    )


def test_a_build_fills_a_free_vmid_and_marks_the_template_last(setup) -> None:
    config, host = setup

    assert builder(config, host).build() == (9201, "2.340.1")

    vm = host.machines[9201]
    assert vm.template and TAG in vm.tags and vm.name == "ci-runner-template-9201"
    assert "import-from=/images/noble.img" in vm.options["scsi0"]
    assert vm.options["net0"] == "virtio,bridge=vmbr1,firewall=1"
    assert vm.options["ipconfig0"] == "ip=192.168.100.20/24,gw=192.168.100.1"
    assert "192.168.100.20" in host.firewall[9201], "the build VM is pinned to its own address"
    assert host.events == [
        "create 9201",
        "resize 9201 scsi0 40G",
        "start 9201",
        "shutdown 9201",
        "template 9201",
    ]
    steps = [step for _, step in host.scripts]
    assert steps == ["probe", "base", "docker", "gh", "node", "dotnet", "go", "python", "finish"]
    assert vm.options["delete"] == "ide2,cicustom,ipconfig0,nameserver"
    marker = decode_marker(vm.description)
    assert marker is not None and marker.runner_version == "2.340.1" and marker.built_at == 5000
    assert not list(host.snippet_dir.glob("*.yaml")), "the cloud-init snippet is removed"


def test_the_next_build_takes_the_other_vmid_and_replaces_its_earlier_build(setup) -> None:
    config, host = setup
    builder(config, host, 1000.0).build()
    assert builder(config, host, 2000.0).build()[0] == 9202

    assert builder(config, host, 3000.0).build()[0] == 9201
    assert "destroy 9201" in host.events
    found = current_template(config.template.vmids, host.vms(), lambda v: host.machines[v].description)
    assert found is not None and found[0] == 9201


def test_a_vmid_held_by_a_vm_the_builder_did_not_create_is_never_touched(setup) -> None:
    config, host = setup
    host.machines[9201] = FakeVm(name="someone-elses-vm", tags={TAG})

    with pytest.raises(PveCiError, match="did not build"):
        builder(config, host).build()

    assert host.machines[9201].name == "someone-elses-vm"
    assert "destroy 9201" not in host.events


@pytest.mark.parametrize("step", ["base", "python", "finish"])
def test_a_failed_step_leaves_no_build_vm_and_no_snippet(setup, step: str) -> None:
    config, host = setup
    host.failing_steps.add(step)

    with pytest.raises(PveCiError):
        builder(config, host).build()

    assert 9201 not in host.machines
    assert not list(host.snippet_dir.glob("*.yaml"))


@pytest.mark.parametrize(("ready_after", "built"), [(2, True), (None, False)])
def test_a_build_waits_for_the_guest_agent_until_its_timeout(
    setup, monkeypatch: pytest.MonkeyPatch, ready_after: int | None, built: bool
) -> None:
    config, host = setup
    now = [5000.0]
    polls: list[int] = []

    def agent_ready(vmid: int) -> bool:
        polls.append(vmid)
        return ready_after is not None and len(polls) > ready_after

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(host, "agent_ready", agent_ready)
    build = TemplateBuilder(
        config, host, clock=lambda: now[0], sleep=sleep, fetch=lambda *_: Path("/images/noble.img")
    )

    if built:
        assert build.build() == (9201, "2.340.1")
        assert len(polls) == 3
    else:
        with pytest.raises(PveCiError, match="did not start its guest agent in time"):
            build.build()
        assert 9201 not in host.machines
        assert now[0] - 5000.0 > 1200


def test_a_build_without_a_runner_version_is_not_a_template(setup) -> None:
    config, host = setup
    host.runner_version = "unknown"

    with pytest.raises(PveCiError, match="runner version"):
        builder(config, host).build()

    assert 9201 not in host.machines


def test_only_the_chosen_components_are_installed_with_their_versions(setup) -> None:
    config, host = setup
    chosen = replace(config.template, components=("node",), versions=(("node", "24.1.0"),))

    builder(replace(config, template=chosen), host).build()

    assert [step for _, step in host.scripts] == ["probe", "base", "node", "finish"]


def test_operator_scripts_run_after_the_components_and_must_be_writable_only_by_root(
    setup, tmp_path: Path
) -> None:
    config, host = setup
    extra = tmp_path / "extra.sh"
    extra.write_text("#!/bin/bash\necho operator\n", encoding="utf-8")
    extra.chmod(0o644)
    with_script = replace(config, template=replace(config.template, scripts=(PurePosixPath(extra),)))

    builder(with_script, host, root=tmp_path).build()
    assert [step for _, step in host.scripts][-2:] == ["operator", "finish"]

    extra.chmod(0o666)
    with pytest.raises(PveCiError, match="writable by no one else"):
        builder(with_script, FakeHost(), root=tmp_path).build()


def test_a_vmid_that_qm_create_refuses_keeps_its_firewall_and_its_vm(setup) -> None:
    config, host = setup
    host.uncreatable.add(9201)

    with pytest.raises(CommandError, match="already exists"):
        builder(config, host).build()

    assert 9201 not in host.firewall
    assert "destroy 9201" not in host.events


def test_the_firewall_is_written_after_create_and_before_boot(setup) -> None:
    config, host = setup
    written = host.write_firewall

    def write_firewall(vmid: int, text: str) -> None:
        assert vmid in host.machines and not host.machines[vmid].running
        written(vmid, text)

    host.write_firewall = write_firewall

    builder(config, host).build()

    assert 9201 in host.firewall


def test_no_build_runs_while_the_datacenter_firewall_is_off(setup) -> None:
    config, host = setup
    host.datacenter_firewall = False

    with pytest.raises(PveCiError, match="datacenter firewall"):
        builder(config, host).build()

    assert host.events == []


def test_a_build_vm_that_reaches_the_host_is_removed_before_anything_is_installed(setup) -> None:
    config, host = setup
    create = host.create

    def create_exposed(vmid: int, options) -> None:
        create(vmid, options)
        host.machines[vmid].isolated = False

    host.create = create_exposed

    with pytest.raises(IsolationError):
        builder(config, host).build()

    assert [step for _, step in host.scripts] == ["probe"]
    assert 9201 not in host.machines


def operator_script(directory: Path) -> Path:
    directory.mkdir(mode=0o755, exist_ok=True)
    file = directory / "extra.sh"
    file.write_text("echo operator\n", encoding="utf-8")
    file.chmod(0o644)
    return file


def test_a_script_in_private_directories_is_read(tmp_path: Path) -> None:
    assert template_build._read_script(operator_script(tmp_path / "scripts"), tmp_path) == "echo operator\n"


def test_a_script_in_a_directory_others_can_write_is_refused(tmp_path: Path) -> None:
    file = operator_script(tmp_path / "scripts")
    file.parent.chmod(0o777)

    with pytest.raises(PveCiError, match="scripts: must be a directory"):
        template_build._read_script(file, tmp_path)


def test_a_symlinked_script_or_directory_is_refused(tmp_path: Path) -> None:
    real = operator_script(tmp_path / "scripts")
    (tmp_path / "link.sh").symlink_to(real)
    (tmp_path / "linked").symlink_to(real.parent)

    with pytest.raises(PveCiError, match="cannot be read"):
        template_build._read_script(tmp_path / "link.sh", tmp_path)
    with pytest.raises(PveCiError, match="linked: must be a directory"):
        template_build._read_script(tmp_path / "linked" / "extra.sh", tmp_path)


def test_a_script_of_another_owner_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = operator_script(tmp_path / "scripts")
    monkeypatch.setattr(os, "geteuid", lambda: file.stat().st_uid + 1)

    with pytest.raises(PveCiError, match="must be a directory owned by root"):
        template_build._read_script(file, tmp_path)


def test_the_image_is_checked_against_its_pinned_digest_and_kept_alone(tmp_path: Path) -> None:
    sha = hashlib.sha256(b"some other image").hexdigest()
    with pytest.raises(PveCiError, match="sha256"):
        fetch_image("https://images.example.org/a.img", sha, tmp_path, opener=lambda _: io.BytesIO(IMAGE))
    assert not list(tmp_path.iterdir())

    real = hashlib.sha256(IMAGE).hexdigest()
    (tmp_path / "old.img").write_bytes(b"older")
    path = fetch_image("https://images.example.org/a.img", real, tmp_path, opener=lambda _: io.BytesIO(IMAGE))
    assert path.read_bytes() == IMAGE
    assert [p.name for p in tmp_path.iterdir()] == [f"{real}.img"]
    again = fetch_image("https://images.example.org/a.img", real, tmp_path, opener=lambda _: io.BytesIO(b""))
    assert again == path, "a verified image is not downloaded again"


def test_an_oversized_or_broken_download_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = hashlib.sha256(IMAGE).hexdigest()
    monkeypatch.setattr(images, "_MAX_BYTES", len(IMAGE) - 1)
    with pytest.raises(PveCiError, match="larger than"):
        fetch_image("https://images.example.org/a.img", real, tmp_path, opener=lambda _: io.BytesIO(IMAGE))
    assert not list(tmp_path.iterdir())

    class Broken(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            if self.tell():
                raise OSError("connection reset")
            return super().read(4)

    with pytest.raises(OSError, match="reset"):
        fetch_image("https://images.example.org/a.img", real, tmp_path, opener=lambda _: Broken(IMAGE))
    assert not list(tmp_path.iterdir())


def test_every_provisioning_script_is_strict_bash() -> None:
    from pve_ci_runners.guest import script
    from tests.fakes import PROVISION

    for name in PROVISION:
        text = script(f"{name}.sh", "provision")
        assert text.startswith("#!/bin/bash\n") and "\nset -euo pipefail\n" in text


def busy_host(tmp_path: Path) -> FakeHost:
    host = FakeHost(total_mb=32000, other_guests_mb=16384)
    host.snippet_dir = tmp_path
    for vmid in (121, 122):
        host.machines[vmid] = FakeVm(name=f"ci-runner-{vmid}", running=True, memory_mb=5120, tags={TAG})
    return host


def test_a_build_drains_the_runners_for_room_and_never_squeezes_other_guests(setup, tmp_path: Path) -> None:
    config, _ = setup
    host = busy_host(tmp_path)
    state_dir = config.daemon.state_dir
    seen: list[tuple[bool, int]] = []

    def controller_retires_the_idle_runners(_: float) -> None:
        seen.append((draining(state_dir), reserved_mb(state_dir)))
        host.machines.pop(121, None)
        host.machines.pop(122, None)

    TemplateBuilder(
        config,
        host,
        clock=lambda: 1.0,
        sleep=controller_retires_the_idle_runners,
        fetch=lambda *_: Path("/i"),
    ).build()

    assert seen == [(True, 4096)], "the runners drain while the build holds its memory"
    assert host.events[0] == "create 9201", "the build VM starts only once it fits"
    assert not draining(state_dir) and reserved_mb(state_dir) == 0


def test_a_build_without_room_gives_up_without_starting_anything(setup, tmp_path: Path) -> None:
    config, _ = setup
    host = busy_host(tmp_path)
    now = [0.0]

    def time_passes(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(PveCiError, match="no room"):
        TemplateBuilder(
            config, host, clock=lambda: now[0], sleep=time_passes, fetch=lambda *_: Path("/i")
        ).build()

    assert not any(e.startswith("create") for e in host.events)
    assert not draining(config.daemon.state_dir) and reserved_mb(config.daemon.state_dir) == 0


def test_the_build_vm_is_not_counted_twice(setup) -> None:
    config, host = setup
    host.total_mb = host.other_guests_mb + config.capacity.host_reserve_mb + config.template.memory_mb

    TemplateBuilder(config, host, clock=lambda: 1.0, sleep=_no_wait, fetch=lambda *_: Path("/i")).build()

    assert "start 9201" in host.events


@pytest.mark.parametrize(("name", "tags"), [("ci-runner-template-9202", {TAG}), ("windows", set())])
def test_a_vm_on_the_other_template_vmid_counts_during_a_build(setup, name: str, tags: set[str]) -> None:
    config, host = setup
    host.total_mb = host.other_guests_mb + config.capacity.host_reserve_mb + config.template.memory_mb
    host.machines[9202] = FakeVm(name=name, memory_mb=1, tags=tags)
    now = [0.0]

    def time_passes(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(PveCiError, match="no room"):
        TemplateBuilder(
            config, host, clock=lambda: now[0], sleep=time_passes, fetch=lambda *_: Path("/i")
        ).build()

    assert not any(e.startswith("create") for e in host.events)


def _no_wait(_: float) -> None:
    raise AssertionError("the build waited for room it already had")


def test_an_operators_drain_outlasts_the_build(setup, tmp_path: Path) -> None:
    config, _ = setup
    host = busy_host(tmp_path)
    drain(config.daemon.state_dir)

    def freed(_: float) -> None:
        host.machines.pop(121, None)
        host.machines.pop(122, None)

    TemplateBuilder(config, host, clock=lambda: 1.0, sleep=freed, fetch=lambda *_: Path("/i")).build()

    assert draining(config.daemon.state_dir)


@pytest.mark.parametrize("running", [True, False])
def test_runner_vms_that_appear_during_the_import_keep_the_build_vm_off(
    setup, monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    config, host = setup
    host.total_mb, host.other_guests_mb = 32000, 16384
    create = host.create

    def controller_starts_runners_meanwhile(vmid: int, options) -> None:
        create(vmid, options)
        for slot in (121, 122):
            host.machines[slot] = FakeVm(
                name=f"ci-runner-{slot}", running=running, memory_mb=5120, tags={TAG}
            )

    monkeypatch.setattr(host, "create", controller_starts_runners_meanwhile)

    with pytest.raises(PveCiError, match="no room"):
        builder(config, host).build()

    assert "start 9201" not in host.events and "destroy 9201" in host.events
    assert not draining(config.daemon.state_dir) and reserved_mb(config.daemon.state_dir) == 0


def test_the_build_vm_starts_under_the_memory_lock_the_controller_takes_too(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, host = setup
    locked: list[bool] = []
    start = host.start

    def recording_start(vmid: int) -> None:
        locked.append(holds_memory_lock(config.daemon.state_dir))
        start(vmid)

    monkeypatch.setattr(host, "start", recording_start)
    builder(config, host).build()

    assert locked == [True]
    assert not holds_memory_lock(config.daemon.state_dir)


def test_a_build_vm_that_cannot_keep_its_out_of_memory_priority_is_removed(setup) -> None:
    config, host = setup
    host.unprotectable.add(9201)

    with pytest.raises(CommandError, match="out-of-memory priority"):
        builder(config, host).build()

    assert "destroy 9201" in host.events
    assert not [s for s in host.scripts if s[1] != "probe"], "no provisioning step runs unprotected"
