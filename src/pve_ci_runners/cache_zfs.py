"""Job caches as ZFS volumes: one golden volume per key, one clone per job.

A clone costs no copy, so every job writes its own cache at full speed. Writing a cache back promotes the
job's clone to become the golden volume; clones other jobs still run on stay valid because they depend on
snapshots that move with the promotion.

A lease comes back from a VM description, which a Proxmox user with VM.Config rights can rewrite, so no
ZFS command runs on a lease that does not name exactly the clone this backend created for that VM, and no
clone is written back for another runner or run than the one it was created for.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pve_ci_runners.cache import Attachment
from pve_ci_runners.config import CacheConfig
from pve_ci_runners.disk import dataset_space
from pve_ci_runners.errors import CommandError
from pve_ci_runners.proc import Commands, output, succeeds
from pve_ci_runners.state import CacheLease

KEY_PROPERTY = "pve-ci-runners:key"
RUNNER_PROPERTY = "pve-ci-runners:runner"
RUN_PROPERTY = "pve-ci-runners:run"
PROMOTING_PROPERTY = "pve-ci-runners:promoting"
USED_PROPERTY = "pve-ci-runners:used"
_IDENTITY = f"{KEY_PROPERTY},{RUNNER_PROPERTY},{RUN_PROPERTY}"
_DEVICE_WAIT = 10.0
log = logging.getLogger("pve_ci_runners")


class ZfsCache:
    """The ZFS cache backend."""

    def __init__(
        self,
        config: CacheConfig,
        commands: Commands,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        device_exists: Callable[[str], bool] = lambda path: Path(path).exists(),
    ) -> None:
        self._config = config
        self._commands = commands
        self._clock = clock
        self._sleep = sleep
        self._device_exists = device_exists
        self.repos = f"{config.dataset}/repos"
        self.jobs = f"{config.dataset}/jobs"
        # Slots finish at the same time; two write-backs of one key must not both pass the origin check.
        self._lock = threading.Lock()

    def lease(self, key: str, vmid: int, *, runner: str, run_id: int) -> Attachment | None:
        """A clone of the newest snapshot of the key's golden volume, created empty on first use.

        Least recently used caches are evicted first when the dataset lacks room for one volume above the
        free reserve. None when even that leaves less room than one volume: clones are sparse, so a job
        could otherwise fill the space the runner VMs write to.
        """
        self._zfs("create", "-p", self.jobs)
        volume = _bytes(self._config.volume_size)
        if self._available() < max(volume, self._free_reserve()):
            self._evict({key} | self._clone_keys(), max(volume, self._free_reserve()))
        available = self._available()
        if available < volume:
            log.warning("the cache dataset has only %d bytes left; the job runs without a cache", available)
            return None
        origin = self._golden_snapshot(key)
        clone = f"{self._clone_prefix(key, vmid)}{int(self._clock())}"
        identity = ("-o", f"{RUNNER_PROPERTY}={runner}", "-o", f"{RUN_PROPERTY}={run_id}")
        self._zfs("clone", "-o", f"{KEY_PROPERTY}={key}", *identity, origin, clone)
        self._zfs("set", f"{USED_PROPERTY}={int(self._clock())}", f"{self.repos}/{key}")
        return Attachment(CacheLease(key=key, clone=clone, origin=origin), self._device(clone))

    def written_bytes(self, lease: CacheLease, vmid: int) -> int:
        """Bytes the job wrote since it got its clone; nothing for a lease this backend did not create."""
        if not self._owned(lease, vmid):
            return 0
        return int(self._zfs("get", "-Hp", "-o", "value", "written", lease.clone).strip() or 0)

    def write_back(self, lease: CacheLease, vmid: int, *, runner: str, run_id: int) -> bool:
        """Promotes the clone to golden unless another job wrote the cache since this lease began.

        The marker property lets `maintain` finish the four steps if the controller stops between them.
        """
        with self._lock:
            return self._write_back(lease, vmid, runner=runner, run_id=run_id)

    def _write_back(self, lease: CacheLease, vmid: int, *, runner: str, run_id: int) -> bool:
        golden = f"{self.repos}/{lease.key}"
        identity = self._identity(lease, vmid)
        if identity is None or self._snapshots(golden)[-1:] != [lease.origin]:
            return False
        if identity != (runner, str(run_id)):
            log.warning("VM %s: its cache clone was leased for another job; not written back", vmid)
            return False
        self._zfs("set", f"{PROMOTING_PROPERTY}={int(self._clock())}", lease.clone)
        # Promote refuses a snapshot name the origin chain already holds, and _expire reads the newest
        # stamp as the last write, so the stamp must be both new and above the origin's.
        stamp = max(int(self._clock()), int(lease.origin.rsplit("@g", 1)[1]) + 1)
        self._zfs("snapshot", f"{lease.clone}@g{stamp}")
        self._zfs("promote", lease.clone)
        self._finish_promotion(lease.clone, golden)
        return True

    def drop(self, lease: CacheLease, vmid: int) -> None:
        """Destroys the job's clone and its snapshots."""
        with self._lock:
            if self._owned(lease, vmid):
                self._commands.run(["zfs", "destroy", "-r", lease.clone], timeout=300)

    def maintain(self, in_use: set[str]) -> list[str]:
        """Finishes interrupted write-backs, removes orphans and expired caches, then frees the dataset.

        A cache a remaining clone was made from is never evicted: its clone depends on its snapshot.
        """
        lines = self._recover()
        lines += self._remove_orphans(in_use)
        lines += self._expire()
        lines += self._evict(self._clone_keys(), self._free_reserve())
        return lines

    def purge(self) -> None:
        """Destroys every job clone, then every cache; the configured dataset itself is the operator's.

        Clones depend on snapshots of the caches, so the clones go first.
        """
        for dataset in (self.jobs, self.repos):
            if succeeds(self._commands, ["zfs", "list", "-H", "-o", "name", dataset]):
                self._zfs("destroy", "-r", dataset)

    def _owned(self, lease: CacheLease, vmid: int) -> bool:
        return self._identity(lease, vmid) is not None

    def _identity(self, lease: CacheLease, vmid: int) -> tuple[str, str] | None:
        """The runner and run of this VM's clone of the key, or None when the clone is not that."""
        clone = re.escape(self._clone_prefix(lease.key, vmid)) + "[0-9]+"
        origin = re.escape(f"{self.repos}/{lease.key}@g") + "[0-9]+"
        if re.fullmatch(clone, lease.clone) and re.fullmatch(origin, lease.origin):
            # Only root can set these properties; they were set when the clone was created.
            found = self._commands.run(
                ["zfs", "get", "-H", "-o", "value", _IDENTITY, lease.clone], timeout=60
            )
            match found.stdout.splitlines():
                case [key, runner, run] if found.returncode == 0 and key == lease.key:
                    return runner, run
                case _:
                    pass
        log.warning("VM %s names a cache clone this controller did not create for it; left alone", vmid)
        return None

    def _clone_prefix(self, key: str, vmid: int) -> str:
        return f"{self.jobs}/{key.replace('/', '.')}-{vmid}-"

    def _golden_snapshot(self, key: str) -> str:
        golden = f"{self.repos}/{key}"
        snapshots = self._snapshots(golden)
        if snapshots:
            return snapshots[-1]
        self._zfs("create", "-p", "-s", "-V", self._config.volume_size, "-o", "volblocksize=16k", golden)
        output(
            self._commands,
            ["mkfs.ext4", "-q", "-L", "ci-cache", "-m", "0", self._device(golden)],
            timeout=300,
        )
        snapshot = f"{golden}@g{int(self._clock())}"
        self._zfs("snapshot", snapshot)
        return snapshot

    def _finish_promotion(self, clone: str, golden: str) -> None:
        self._commands.run(["zfs", "destroy", "-r", golden], timeout=300)
        self._zfs("rename", clone, golden)
        self._zfs("inherit", PROMOTING_PROPERTY, golden)
        for name in (KEY_PROPERTY, RUNNER_PROPERTY, RUN_PROPERTY):
            self._zfs("inherit", name, golden)
        for old in self._snapshots(golden)[: -self._config.keep_snapshots]:
            # A snapshot a running job's clone still depends on cannot go yet; the next write-back retries.
            self._commands.run(["zfs", "destroy", old], timeout=300)

    def _recover(self) -> list[str]:
        lines: list[str] = []
        for clone, marker, key in self._job_volumes():
            if marker in {"", "-"}:
                continue
            promoted = self._zfs("get", "-H", "-o", "value", "origin", clone).strip() in {"", "-"}
            if promoted and key not in {"", "-"}:
                self._finish_promotion(clone, f"{self.repos}/{key}")
                lines.append(f"finished the interrupted write-back of the cache {key}")
            else:
                self._commands.run(["zfs", "destroy", "-r", clone], timeout=300)
                lines.append(f"dropped the half-promoted clone {clone}")
        return lines

    def _remove_orphans(self, in_use: set[str]) -> list[str]:
        lines: list[str] = []
        for clone, _, _ in self._job_volumes():
            if clone not in in_use and succeeds(self._commands, ["zfs", "destroy", "-r", clone]):
                lines.append(f"removed the orphaned cache clone {clone}")
        return lines

    def _expire(self) -> list[str]:
        goldens = self._volumes(self.repos, recursive=True)
        limit = self._clock() - self._config.retention_days * 86400
        return [
            f"expired the cache {g}, unused for {self._config.retention_days} days"
            for g in goldens
            if self._written(g) < limit and succeeds(self._commands, ["zfs", "destroy", "-r", g])
        ]

    def _evict(self, protected: set[str], reserve: int) -> list[str]:
        """Destroys the least recently used caches until the dataset and the pool have room again."""
        lines: list[str] = []
        for golden in self._least_recently_used(protected):
            shortage = self._shortage(reserve)
            if not shortage:
                break
            if succeeds(self._commands, ["zfs", "destroy", "-r", golden]):
                lines.append(f"{shortage}: evicted the cache {golden}")
        return lines

    def _shortage(self, reserve: int) -> str:
        """Why the cache must shrink, or nothing; the pool's capacity bounds the dataset's free space."""
        available = self._available()
        if available < reserve:
            return f"only {available} bytes available, {reserve} reserved"
        capacity = int(self._zpool_capacity(self._config.dataset.split("/", 1)[0]))
        return f"pool at {capacity}%" if capacity >= self._config.pool_high_water_percent else ""

    def _least_recently_used(self, protected: set[str]) -> list[str]:
        listing = self._commands.run(
            ["zfs", "list", "-H", "-p", "-o", f"name,{USED_PROPERTY}", "-r", "-t", "volume", self.repos],
            timeout=60,
        )
        rows = [line.split("\t") for line in listing.stdout.splitlines() if line.strip()]
        prefix = f"{self.repos}/"
        used = {
            r[0]: max(int(r[1]) if r[1].isdecimal() else 0, self._written(r[0]))
            for r in rows
            if len(r) == 2 and r[0].removeprefix(prefix) not in protected  # noqa: PLR2004 - two requested columns
        }
        return sorted(used, key=used.__getitem__)

    def _written(self, golden: str) -> int:
        """When the cache was last written back: its newest snapshot carries the time."""
        return int((self._snapshots(golden) or ["@g0"])[-1].rsplit("@g", 1)[-1] or 0)

    def _clone_keys(self) -> set[str]:
        return {key for _, _, key in self._job_volumes()}

    def _available(self) -> int:
        return dataset_space(self._commands, self._config.dataset).available

    def _free_reserve(self) -> int:
        return self._config.free_reserve_gb * 2**30

    def _job_volumes(self) -> list[tuple[str, str, str]]:
        listing = self._commands.run(
            [
                "zfs",
                "list",
                "-H",
                "-o",
                f"name,{PROMOTING_PROPERTY},{KEY_PROPERTY}",
                "-d",
                "1",
                "-t",
                "volume",
                self.jobs,
            ],
            timeout=60,
        )
        rows = [line.split("\t") for line in listing.stdout.splitlines() if line.strip()]
        return [(r[0], r[1], r[2]) for r in rows if len(r) == 3]  # noqa: PLR2004 - three requested columns

    def _volumes(self, dataset: str, *, recursive: bool) -> list[str]:
        depth = ["-r"] if recursive else ["-d", "1"]
        listing = self._commands.run(
            ["zfs", "list", "-H", "-o", "name", *depth, "-t", "volume", dataset], timeout=60
        )
        return [line for line in listing.stdout.split() if line]

    def _snapshots(self, dataset: str) -> list[str]:
        listing = self._commands.run(
            ["zfs", "list", "-H", "-t", "snapshot", "-o", "name", "-s", "creation", "-d", "1", dataset],
            timeout=60,
        )
        return [line for line in listing.stdout.splitlines() if line] if listing.returncode == 0 else []

    def _zpool_capacity(self, pool: str) -> str:
        return (
            output(self._commands, ["zpool", "list", "-H", "-o", "capacity", pool], timeout=60)
            .strip()
            .rstrip("%")
        )

    def _device(self, volume: str) -> str:
        path = f"/dev/zvol/{volume}"
        deadline = self._clock() + _DEVICE_WAIT
        while not self._device_exists(path):
            if self._clock() > deadline:
                raise CommandError(f"{path} did not appear")
            self._sleep(0.2)
        return path

    def _zfs(self, *args: str) -> str:
        return output(self._commands, ["zfs", *args], timeout=300)


def _bytes(size: str) -> int:
    return int(size[:-1]) * {"M": 2**20, "G": 2**30}[size[-1]]
