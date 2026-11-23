"""The ZFS cache backend against a real pool on a file, run as root in CI (`pytest -m zfs`).

When `PVE_CI_REQUIRE_ZFS` is set, a host without a working ZFS module fails the run instead of skipping it,
so CI can never pass without having exercised the real backend.
"""

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from pve_ci_runners.cache_zfs import PROMOTING_PROPERTY, RUNNER_PROPERTY, ZfsCache
from pve_ci_runners.config import CacheConfig, Config
from pve_ci_runners.proc import HostCommands

pytestmark = pytest.mark.zfs
RUNNER = "ci-runner-121-abc123"


def _sh(*argv: str) -> str:
    return subprocess.run(list(argv), capture_output=True, text=True, check=True).stdout


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[str]:
    usable = (
        os.geteuid() == 0
        and shutil.which("zpool")
        and subprocess.run(["modprobe", "zfs"], check=False).returncode == 0
    )
    if not usable:
        if os.environ.get("PVE_CI_REQUIRE_ZFS"):
            pytest.fail("ZFS is required but not usable on this host")
        pytest.skip("needs root and the ZFS kernel module")
    name = f"cit{uuid.uuid4().hex[:8]}"
    backing = tmp_path / "pool.img"
    _sh("truncate", "-s", "2G", str(backing))
    _sh("zpool", "create", "-o", "cachefile=none", "-m", "none", name, str(backing))
    try:
        yield name
    finally:
        subprocess.run(["zpool", "destroy", "-f", name], check=False)


def _settings(config: Config, dataset: str, volume_size: str, free_reserve_gb: int = 0) -> CacheConfig:
    """The test pool holds 2 GiB, far below the default free reserve, which would evict every cache."""
    return replace(config.cache, dataset=dataset, volume_size=volume_size, free_reserve_gb=free_reserve_gb)


def _write(device: str, mountpoint: Path, name: str, mebibytes: int = 2) -> None:
    _sh("udevadm", "settle")
    mountpoint.mkdir(exist_ok=True)
    _sh("mount", device, str(mountpoint))
    (mountpoint / name).write_bytes(os.urandom(mebibytes * 2**20))
    _sh("umount", str(mountpoint))


def _files(device: str, mountpoint: Path) -> set[str]:
    _sh("udevadm", "settle")
    _sh("mount", "-o", "ro", device, str(mountpoint))
    try:
        return {p.name for p in mountpoint.iterdir() if p.name != "lost+found"}
    finally:
        _sh("umount", str(mountpoint))


def test_a_written_back_cache_is_what_the_next_job_starts_with(
    pool: str, config: Config, tmp_path: Path
) -> None:
    cache = ZfsCache(_settings(config, f"{pool}/ci", "64M"), HostCommands())
    first = cache.lease("o__r/ci__build", 121, runner=RUNNER, run_id=2)
    assert first is not None
    _write(first.device, tmp_path / "m", "from-main")

    assert cache.written_bytes(first.lease, 121) >= 2**20
    assert not cache.write_back(first.lease, 121, runner="ci-runner-121-aaaaaa", run_id=1)
    assert not cache.write_back(first.lease, 121, runner=RUNNER, run_id=1)
    assert cache.write_back(first.lease, 121, runner=RUNNER, run_id=2)
    golden = f"{pool}/ci/repos/o__r/ci__build"
    assert _sh("zfs", "get", "-H", "-o", "source", RUNNER_PROPERTY, golden).strip() != "local"

    second = cache.lease("o__r/ci__build", 122, runner=RUNNER, run_id=3)
    assert second is not None
    assert _files(second.device, tmp_path / "m") == {"from-main"}
    cache.drop(second.lease, 122)
    assert second.lease.clone not in _sh("zfs", "list", "-H", "-o", "name", "-r", f"{pool}/ci/jobs")


def test_a_stale_lease_does_not_overwrite_a_newer_cache(pool: str, config: Config, tmp_path: Path) -> None:
    cache = ZfsCache(_settings(config, f"{pool}/ci", "64M"), HostCommands())
    early = cache.lease("o__r/ci__build", 121, runner=RUNNER, run_id=1)
    late = cache.lease("o__r/ci__build", 122, runner=RUNNER, run_id=2)
    assert early is not None and late is not None
    _write(late.device, tmp_path / "m", "late")
    assert cache.write_back(late.lease, 122, runner=RUNNER, run_id=2)

    assert not cache.write_back(early.lease, 121, runner=RUNNER, run_id=1)
    cache.drop(early.lease, 121)


def test_maintenance_finishes_a_promotion_interrupted_before_the_rename(
    pool: str, config: Config, tmp_path: Path
) -> None:
    dataset = f"{pool}/ci"
    cache = ZfsCache(_settings(config, dataset, "64M"), HostCommands())
    lease = cache.lease("o__r/ci__build", 121, runner=RUNNER, run_id=1)
    assert lease is not None
    _write(lease.device, tmp_path / "m", "interrupted")
    golden = f"{dataset}/repos/o__r/ci__build"
    _sh("zfs", "set", f"{PROMOTING_PROPERTY}=1", lease.lease.clone)
    stamp = int(lease.lease.origin.rsplit("@g", 1)[1]) + 1
    _sh("zfs", "snapshot", f"{lease.lease.clone}@g{stamp}")
    _sh("zfs", "promote", lease.lease.clone)
    _sh("zfs", "destroy", "-r", golden)

    lines = cache.maintain(set())

    assert any("finished the interrupted write-back" in line for line in lines)
    following = cache.lease("o__r/ci__build", 122, runner=RUNNER, run_id=2)
    assert following is not None
    assert _files(following.device, tmp_path / "m") == {"interrupted"}


def test_a_trusted_clone_is_promoted_although_the_golden_fills_most_of_a_volume(
    pool: str, config: Config, tmp_path: Path
) -> None:
    cache = ZfsCache(_settings(config, f"{pool}/ci", "64M"), HostCommands())
    seed = cache.lease("o__r/ci__build", 121, runner=RUNNER, run_id=1)
    assert seed is not None
    _write(seed.device, tmp_path / "m", "seed", 38)
    assert cache.write_back(seed.lease, 121, runner=RUNNER, run_id=1)

    trusted = cache.lease("o__r/ci__build", 121, runner=RUNNER, run_id=2)
    assert trusted is not None
    _write(trusted.device, tmp_path / "m", "seed", 32)

    assert cache.write_back(trusted.lease, 121, runner=RUNNER, run_id=2)


def test_a_pool_without_room_for_one_more_volume_gives_no_cache(pool: str, config: Config) -> None:
    cache = ZfsCache(_settings(config, f"{pool}/ci", "4G"), HostCommands())

    assert cache.lease("o__r/ci__build", 121, runner=RUNNER, run_id=1) is None
    assert "o__r" not in _sh("zfs", "list", "-H", "-o", "name", "-r", f"{pool}/ci")


def test_maintenance_evicts_an_unused_cache_but_never_one_a_job_still_uses(pool: str, config: Config) -> None:
    cache = ZfsCache(_settings(config, f"{pool}/ci", "64M", free_reserve_gb=1000), HostCommands())
    busy = cache.lease("o__r/ci__busy", 121, runner=RUNNER, run_id=1)
    idle = cache.lease("o__r/ci__idle", 122, runner=RUNNER, run_id=2)
    assert busy is not None and idle is not None
    cache.drop(idle.lease, 122)

    lines = cache.maintain({busy.lease.clone})

    names = _sh("zfs", "list", "-H", "-o", "name", "-r", f"{pool}/ci/repos").split()
    assert f"{pool}/ci/repos/o__r/ci__busy" in names, "its snapshot carries a running job's clone"
    assert f"{pool}/ci/repos/o__r/ci__idle" not in names
    assert any(line.endswith(f"evicted the cache {pool}/ci/repos/o__r/ci__idle") for line in lines)
