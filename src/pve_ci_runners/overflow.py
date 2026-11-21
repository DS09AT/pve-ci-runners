"""Sending jobs to a hosted runner while the self-hosted pool is full.

Workflows choose their runner through a repository variable, read with `fromJSON(vars.NAME || ...)`.
The controller flips that variable for the configured repositories only; a job that names a runner
directly is never redirected. GitHub evaluates `runs-on` when a job is queued, so only jobs queued after
a switch move; the hysteresis keeps the variable from flapping between neighbouring polls.
"""

from dataclasses import dataclass
from typing import Protocol

from pve_ci_runners.config import OverflowConfig
from pve_ci_runners.errors import GitHubError


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


def decide(
    state: Hysteresis, *, unserved: int, oldest_wait: float, now: float, config: OverflowConfig
) -> Hysteresis:
    """Switches on as soon as jobs back up and off only after a calm period."""
    if unserved >= config.on_backlog or oldest_wait >= config.on_wait_seconds:
        return Hysteresis(on=True)
    if not state.on:
        return Hysteresis()
    if unserved > 0:
        return Hysteresis(on=True)
    calm_since = state.calm_since if state.calm_since is not None else now
    return Hysteresis(on=now - calm_since < config.off_seconds, calm_since=calm_since)


class Overflow:
    """Keeps the variable of every configured repository in line with the decision."""

    def __init__(self, config: OverflowConfig, writer: VariableWriter) -> None:
        self._config = config
        self._writer = writer
        self._state = Hysteresis()
        self._written: dict[str, str] = {}

    @property
    def on(self) -> bool:
        """Whether jobs are currently being sent to the hosted runner."""
        return self._state.on

    def update(self, *, unserved: int, oldest_wait: float, now: float, forced: bool = False) -> list[str]:
        """Applies the decision; returns one log line per change and one per failed write."""
        before = self._state.on
        self._state = (
            Hysteresis(on=True)
            if forced
            else decide(self._state, unserved=unserved, oldest_wait=oldest_wait, now=now, config=self._config)
        )
        lines: list[str] = []
        if self._state.on != before:
            lines.append(f"overflow {'on' if self._state.on else 'off'}: {unserved} jobs without a VM")
        value = self._config.hosted_value if self._state.on else self._config.self_value
        for repository in self._config.repositories:
            if self._written.get(repository) == value:
                continue
            try:
                self._writer.set_variable(repository, self._config.variable, value)
            except GitHubError as err:
                lines.append(f"{repository}: {self._config.variable} not updated: {err}")
                continue
            self._written[repository] = value
            lines.append(f"{repository}: {self._config.variable} = {value}")
        return lines
