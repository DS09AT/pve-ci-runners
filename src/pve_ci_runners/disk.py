"""Free disk space where runner VMs and job caches write: the ZFS datasets behind them.

The pool's own capacity says nothing about a quota on the dataset the runners live in. ZFS reports in
`available` what is left for writes there after the dataset's quota and refquota, the quotas of its
ancestors and the pool's free space, so that value is what every check here relies on.
"""

import json
from dataclasses import dataclass

from pve_ci_runners.config import Config
from pve_ci_runners.errors import CommandError
from pve_ci_runners.proc import Commands, output

_PROPERTIES = ("available", "used", "quota")


@dataclass(frozen=True)
class DatasetSpace:
    """What `zfs get` reports for one dataset; a quota of 0 means none."""

    dataset: str
    available: int
    used: int
    quota: int


def dataset_space(commands: Commands, dataset: str) -> DatasetSpace:
    """The space of one dataset; output that does not name every value as a number is an error."""
    text = output(
        commands,
        ["zfs", "get", "-Hp", "-o", "property,value", ",".join(_PROPERTIES), dataset],
        timeout=60,
    )
    values = dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)
    if not all(values.get(name, "").isdecimal() for name in _PROPERTIES):
        raise CommandError(f"zfs get reported no space for {dataset}")
    return DatasetSpace(dataset, int(values["available"]), int(values["used"]), int(values["quota"]))


class ZfsSpace:
    """The datasets of the template storage and the cache, measured on every call."""

    def __init__(self, commands: Commands, *, storage: str, cache_dataset: str | None) -> None:
        self._commands = commands
        self._storage = storage
        self._cache_dataset = cache_dataset

    def spaces(self) -> list[DatasetSpace]:
        """Every dataset runners write to; empty when neither the storage nor the cache is on ZFS."""
        datasets = dict.fromkeys(d for d in (self._storage_dataset(), self._cache_dataset) if d)
        return [dataset_space(self._commands, d) for d in datasets]

    def available(self) -> int | None:
        """The least free space of those datasets, or None when there is no dataset to measure."""
        spaces = self.spaces()
        return min(s.available for s in spaces) if spaces else None

    def _storage_dataset(self) -> str | None:
        argv = ["pvesh", "get", f"/storage/{self._storage}", "--output-format", "json"]
        try:
            info = json.loads(output(self._commands, argv, timeout=60))
        except ValueError:
            raise CommandError(f"pvesh get /storage/{self._storage} returned no JSON") from None
        if not isinstance(info, dict) or info.get("type") != "zfspool":
            return None
        pool = info.get("pool")
        if not isinstance(pool, str) or not pool:
            raise CommandError(f"the storage {self._storage} names no ZFS dataset")
        return pool


def disk_space(config: Config, commands: Commands) -> ZfsSpace:
    """The space the configured template storage and cache write to."""
    cache = config.cache
    return ZfsSpace(
        commands,
        storage=config.template.storage,
        cache_dataset=cache.dataset if cache.backend == "zfs" else None,
    )
