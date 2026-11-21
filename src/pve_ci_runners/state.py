"""The state of one runner VM, kept as JSON in the VM's description.

Storing it on the VM itself makes the controller crash-only: after a restart it rebuilds everything from
the host. The description is also writable by anyone with VM.Config rights, so it is parsed as untrusted
input and anything malformed is treated as not ours.
"""

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Self, cast

from pve_ci_runners.config import NAME, REPOSITORY

FORMAT = 1
PHASES = frozenset({"booting", "warm", "assigned"})
_RUNNER = r"[a-z][a-z0-9-]{0,80}"
COMMIT = r"[0-9a-f]{40}"
_DATASET = r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,250}"
_DESCRIPTION_LIMIT = 8192


@dataclass(frozen=True)
class CacheLease:
    """The cache clone a job works on and the snapshot it was cloned from."""

    key: str
    clone: str
    origin: str


@dataclass(frozen=True)
class VerifiedJob:
    """The job a runner serves, as the GitHub API reports it, never as the guest does."""

    run_id: int
    job: str
    workflow: str
    branch: str
    event: str
    same_repository: bool
    head_sha: str


@dataclass(frozen=True)
class VmState:
    """Everything the controller needs to know about one slot VM."""

    phase: str
    since: int
    profile: str
    payload: str
    repository: str | None = None
    runner: str | None = None
    job_started: int | None = None
    done_at: int | None = None
    cache: CacheLease | None = None
    verified: VerifiedJob | None = None
    running: bool = field(default=False, compare=False)

    def evolve(self, **changes: object) -> Self:
        """A copy with the given fields changed."""
        return replace(self, **changes)  # type: ignore[arg-type]  # dataclasses.replace takes typed kwargs

    def encode(self) -> str:
        """The description text for this state."""
        data = {k: v for k, v in asdict(self).items() if k != "running" and v is not None}
        text = json.dumps({"format": FORMAT, **data}, separators=(",", ":"), sort_keys=True)
        if len(text) > _DESCRIPTION_LIMIT:
            raise ValueError("VM state does not fit into a description")
        return text


def decode(description: str, *, running: bool) -> VmState | None:
    """The state stored in a description, or None when it is not a valid state of this controller."""
    try:
        data = json.loads(description)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return _state(cast("dict[str, object]", data), running=running)
    except (KeyError, TypeError, ValueError):
        return None


def _state(data: dict[str, object], *, running: bool) -> VmState:
    if data.get("format") != FORMAT or data.get("phase") not in PHASES:
        raise ValueError("not a state of this controller")
    cache = data.get("cache")
    verified = data.get("verified")
    return VmState(
        phase=_text(data, "phase", r"[a-z]+"),
        since=_number(data, "since"),
        profile=_text(data, "profile", NAME),
        payload=_text(data, "payload", r"[0-9a-f]{16,64}"),
        repository=_optional_text(data, "repository", REPOSITORY),
        runner=_optional_text(data, "runner", _RUNNER),
        job_started=_optional_number(data, "job_started"),
        done_at=_optional_number(data, "done_at"),
        cache=_lease(cast("dict[str, object]", cache)) if isinstance(cache, dict) else None,
        verified=_verified(cast("dict[str, object]", verified)) if isinstance(verified, dict) else None,
        running=running,
    )


def _lease(data: dict[str, object]) -> CacheLease:
    return CacheLease(
        key=_text(data, "key", r"[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,170}"),
        clone=_text(data, "clone", _DATASET),
        origin=_text(data, "origin", _DATASET),
    )


def _verified(data: dict[str, object]) -> VerifiedJob:
    same = data["same_repository"]
    if not isinstance(same, bool):
        raise TypeError("same_repository")
    return VerifiedJob(
        run_id=_number(data, "run_id"),
        job=_text(data, "job", r".{1,200}"),
        workflow=_text(data, "workflow", r"[A-Za-z0-9_./-]{1,250}"),
        branch=_text(data, "branch", r".{0,250}"),
        event=_text(data, "event", r"[a-z_]{1,50}"),
        same_repository=same,
        head_sha=_text(data, "head_sha", COMMIT),
    )


def _text(data: dict[str, object], key: str, pattern: str) -> str:
    value = data[key]
    if not isinstance(value, str) or re.fullmatch(pattern, value, flags=re.DOTALL) is None:
        raise ValueError(key)
    return value


def _optional_text(data: dict[str, object], key: str, pattern: str) -> str | None:
    return _text(data, key, pattern) if key in data else None


def _number(data: dict[str, object], key: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(key)
    return value


def _optional_number(data: dict[str, object], key: str) -> int | None:
    return _number(data, key) if key in data else None
