"""Sending jobs to a hosted runner while the self-hosted pool is full.

Workflows choose their runner through a repository variable, read with `fromJSON(vars.NAME || ...)`.
The controller flips that variable for the configured repositories only; a job that names a runner
directly is never redirected. GitHub reads `vars` once, when a workflow run is created, so every job of a
run keeps the value of that moment, even one queued long after a switch. Overflow therefore switches on
while the pool still has room, before a run created now could lock onto busy slots, and off only after
the pool has had room for a while; the hysteresis keeps the variable from flapping between polls.
"""

from dataclasses import dataclass
from typing import Protocol

from pve_ci_runners.config import OverflowConfig
from pve_ci_runners.errors import GitHubError

# Another controller or a manual edit can overwrite the variable; an unchanged value is written again
# after this long, so such a change lasts one interval at most.
REASSERT_SECONDS = 600


class VariableWriter(Protocol):
    """Writes one Actions variable of a repository."""

    def set_variable(self, repository: str, name: str, value: str) -> None:
        """Creates or updates the variable."""
        ...


@dataclass(frozen=True)
class Hysteresis:
    """Whether overflow is on and since when nothing has been waiting."""

    on: bool = False
    calm_since: float | None = None


def trigger(*, unserved: int, oldest_wait: float, free: int, config: OverflowConfig) -> str | None:
    """Why the pool needs the hosted runner now, or None while it copes on its own.

    `on_backlog = 0` and `on_free_slots = -1` turn their trigger off, so only a real wait costs hosted
    minutes.
    """
    if config.on_backlog and unserved >= config.on_backlog:
        return f"{unserved} jobs without a VM"
    if oldest_wait >= config.on_wait_seconds:
        return f"a job waits for {oldest_wait:.0f} s"
    if config.on_free_slots >= 0 and free <= config.on_free_slots:
        return f"{free} slot{'' if free == 1 else 's'} free"
    return None


def decide(
    state: Hysteresis, *, unserved: int, oldest_wait: float, free: int, now: float, config: OverflowConfig
) -> Hysteresis:
    """Switches on as soon as jobs back up or the pool runs low, off only after a calm period with room.

    `free` is the number of further jobs the pool could start at once.
    """
    if trigger(unserved=unserved, oldest_wait=oldest_wait, free=free, config=config) is not None:
        return Hysteresis(on=True)
    if not state.on:
        return Hysteresis()
    if unserved > 0 or free < config.off_free_slots:
        return Hysteresis(on=True)
    calm_since = state.calm_since if state.calm_since is not None else now
    return Hysteresis(on=now - calm_since < config.off_seconds, calm_since=calm_since)


class Overflow:
    """Keeps the variable of every configured repository in line with the decision."""

    def __init__(self, config: OverflowConfig, writer: VariableWriter) -> None:
        self._config = config
        self._writer = writer
        self._state = Hysteresis()
        self._written: dict[str, tuple[str, float]] = {}

    @property
    def on(self) -> bool:
        """Whether jobs are currently being sent to the hosted runner."""
        return self._state.on

    def hosted(self, repository: str) -> bool:
        """Whether the repository's variable was last written with the hosted value."""
        written = self._written.get(repository)
        return written is not None and written[0] == self._config.hosted_value

    def update(
        self, *, unserved: int, oldest_wait: float, free: int, now: float, forced: bool = False
    ) -> list[str]:
        """Applies the decision; returns one log line per switch, per write and per failed write."""
        before = self._state.on
        self._state = (
            Hysteresis(on=True)
            if forced
            else decide(
                self._state,
                unserved=unserved,
                oldest_wait=oldest_wait,
                free=free,
                now=now,
                config=self._config,
            )
        )
        lines: list[str] = []
        if self._state.on != before:
            reason = trigger(unserved=unserved, oldest_wait=oldest_wait, free=free, config=self._config)
            lines.append(self._switched(reason="no VM can start" if forced else reason, free=free))
        value = self._config.hosted_value if self._state.on else self._config.self_value
        lines.extend(
            self._write(repository, value, now)
            for repository in self._config.repositories
            if not self._current(repository, value, now)
        )
        return lines

    def _switched(self, *, reason: str | None, free: int) -> str:
        if self._state.on:
            return f"overflow on: {reason}"
        return f"overflow off: {free} slots free and nothing unserved for {self._config.off_seconds} s"

    def _current(self, repository: str, value: str, now: float) -> bool:
        written = self._written.get(repository)
        return written is not None and written[0] == value and now - written[1] < REASSERT_SECONDS

    def _write(self, repository: str, value: str, now: float) -> str:
        try:
            self._writer.set_variable(repository, self._config.variable, value)
        except GitHubError as err:
            return f"{repository}: {self._config.variable} not updated: {err}"
        self._written[repository] = (value, now)
        return f"{repository}: {self._config.variable} = {value}"
