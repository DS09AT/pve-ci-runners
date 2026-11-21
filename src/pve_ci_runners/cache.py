"""The cache a job gets: its key, the backend interface and the backend that keeps nothing."""

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from pve_ci_runners.state import CacheLease

_UNSAFE = re.compile(r"[^a-z0-9_.-]")
_READABLE = 60
_DIGEST = 32


def cache_key(repository: str, workflow: str, job: str) -> str:
    """One cache per workflow job, inside a namespace of its repository.

    The repository part comes first and alone, so no workflow or job name, which a pull request can
    choose, can reach another repository's caches. Cleaning and truncating make names readable but not
    unique, so each part ends in a digest of the exact names it stands for.
    """
    workflow_name = workflow.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    namespace = f"{_clean(repository.replace('/', '__'))}-{_digest(repository.lower())}"
    exact = f"{workflow}\0{job}"
    job_part = f"{_clean(workflow_name)}__{_clean(job)}-{_digest(exact)}"
    return f"{namespace}/{job_part}"


def _clean(text: str) -> str:
    return _UNSAFE.sub("-", text.lower()).strip(".-")[:_READABLE] or "unnamed"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:_DIGEST]


@dataclass(frozen=True)
class Attachment:
    """A lease and the host block device that carries it."""

    lease: CacheLease
    device: str


class CacheBackend(Protocol):
    """Gives each job its own writable copy of a cache and keeps the store bounded."""

    def lease(self, key: str, vmid: int, *, runner: str, run_id: int) -> Attachment | None:
        """A private copy of the newest cache for the key for one job, or None when there is none to give."""
        ...

    def written_bytes(self, lease: CacheLease, vmid: int) -> int:
        """How much the job of this VM changed in its copy."""
        ...

    def write_back(self, lease: CacheLease, vmid: int, *, runner: str, run_id: int) -> bool:
        """Makes the copy the cache; False when another job wrote it since or the copy is another job's."""
        ...

    def drop(self, lease: CacheLease, vmid: int) -> None:
        """Discards the copy."""
        ...

    def maintain(self, in_use: set[str]) -> list[str]:
        """Finishes interrupted write-backs, removes orphans and expired caches; returns log lines."""
        ...


class NoCache:
    """For hosts without a supported store: every job starts empty."""

    def lease(self, key: str, vmid: int, *, runner: str, run_id: int) -> Attachment | None:  # noqa: ARG002, PLR6301 - interface method
        """No cache."""
        return None

    def written_bytes(self, lease: CacheLease, vmid: int) -> int:  # noqa: ARG002, PLR6301 - interface method
        """Nothing is ever written."""
        return 0

    def write_back(self, lease: CacheLease, vmid: int, *, runner: str, run_id: int) -> bool:  # noqa: ARG002, PLR6301 - interface method
        """Nothing to keep."""
        return False

    def drop(self, lease: CacheLease, vmid: int) -> None:
        """Nothing to drop."""

    def maintain(self, in_use: set[str]) -> list[str]:  # noqa: ARG002, PLR6301 - interface method
        """Nothing to maintain."""
        return []
