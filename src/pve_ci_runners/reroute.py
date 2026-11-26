"""Moving runs that locked onto the self-hosted pool before overflow switched on to the hosted runner.

GitHub reads `vars` when a run attempt is created, so a burst that arrives while the pool is idle binds
every job to the self-hosted labels before overflow can react, and the surplus waits for a free slot. A
new attempt reads the variable again: while the variable points at the hosted runner and the overflow
profile has unserved jobs, a run whose job waited too long is cancelled and, once GitHub has recorded the
cancellation, its cancelled and failed jobs are run again; finished jobs stay as they are.

A run qualifies when no runner has picked up a job of it yet, or, for a pull request, when every job a
runner holds started at most `reroute_discard_seconds` ago: those jobs are cut short and run again, on
the assumption that pull request CI does not deploy or publish; leave reroute_discard_seconds at 0
otherwise. Other events never lose a started job,
and a re-run is refused if a job of theirs ran after all. Jobs with `if: always()` or `cancelled()` run on
the cancellation. A newer run of the same workflow and branch refuses every re-run, so an older commit
never overtakes a newer one.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from pve_ci_runners.actions import QueuedJob, RunSummary
from pve_ci_runners.config import OverflowConfig
from pve_ci_runners.errors import GitHubError

# Bounds the cancellations of one cycle; the next cycle takes the rest.
PER_CYCLE = 5
_HTTP_FORBIDDEN = 403
log = logging.getLogger("pve_ci_runners")


class RunControl(Protocol):
    """Cancels and re-runs workflow runs."""

    def summary(self, repository: str, run_id: int) -> RunSummary:
        """The run as GitHub shows it right now."""
        ...

    def cancel(self, repository: str, run_id: int) -> str | None:
        """Cancels the run and returns None, or returns why it is left alone."""
        ...

    def rerun_safely(self, repository: str, run_id: int, *, strict: bool) -> str | None:
        """Re-runs the cancelled and failed jobs and returns None, or returns why that would be unsafe."""
        ...

    def run_conclusion(self, repository: str, run_id: int) -> str | None:
        """The run's conclusion, or None while it is not completed."""
        ...


@dataclass(frozen=True)
class _Stalled:
    repository: str
    jobs: int
    waited: float
    discarded: int = 0
    strict: bool = True


class Reroute:
    """Cancels stalled runs and re-runs them, each run at most once per controller lifetime."""

    def __init__(
        self, config: OverflowConfig, control: RunControl, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._clock = clock
        self._after = config.reroute_after_seconds
        self._discard = config.reroute_discard_seconds
        decoded: object = json.loads(config.self_value)
        listed = decoded if isinstance(decoded, list) else [decoded]
        self._labels = frozenset(str(label).lower() for label in listed)
        self._control = control
        self._handled: set[int] = set()
        self._cancelled: dict[int, _Stalled] = {}
        self._denied = False

    def update(self, queued: dict[str, list[QueuedJob]], now: float) -> None:
        """Re-runs what an earlier cycle cancelled, then cancels the stalled runs of these repositories.

        `queued` holds only repositories whose variable points at the hosted runner right now.
        """
        if not self._after or self._denied:
            return
        self._finish_all()
        cancelled = 0
        for run_id, stalled in self._stalled(queued, now).items():
            if self._denied or cancelled >= PER_CYCLE:
                return
            cancelled += self._cancel(run_id, stalled)

    def _stalled(self, queued: dict[str, list[QueuedJob]], now: float) -> dict[int, _Stalled]:
        found: dict[int, _Stalled] = {}
        for repository, jobs in queued.items():
            for job in jobs:
                if not self._candidate(job, now):
                    continue
                known = found.get(job.run_id, _Stalled(repository, 0, 0.0))
                waited = max(known.waited, now - job.created_at)
                found[job.run_id] = _Stalled(repository, known.jobs + 1, waited)
        return found

    def _candidate(self, job: QueuedJob, now: float) -> bool:
        if job.run is None or job.run_id in self._handled or not self._qualifies(job.run, now):
            return False
        bound_here = {label.lower() for label in job.labels} == self._labels
        return bound_here and now - job.created_at >= self._after

    def _qualifies(self, run: RunSummary, now: float) -> bool:
        if not run.complete:
            return False
        if run.unstarted:
            return True
        if run.event != "pull_request" or not self._discard:
            return False
        return all(now - started <= self._discard for started in run.running)

    def _cancel(self, run_id: int, stalled: _Stalled) -> int:
        self._handled.add(run_id)
        try:
            run = self._control.summary(stalled.repository, run_id)
            # The cycle may have spent a while on other runs, so the bound is checked against the clock.
            if not self._qualifies(run, self._clock()):
                log.info("%s run %s not rerouted: it no longer qualifies", stalled.repository, run_id)
                return 0
            refusal = self._control.cancel(stalled.repository, run_id)
        except GitHubError as err:
            self._refused(err, f"{stalled.repository} run {run_id} not cancelled")
            return 0
        if refusal is not None:
            log.info("%s run %s not rerouted: %s", stalled.repository, run_id, refusal)
            return 0
        strict = run.event != "pull_request"
        self._cancelled[run_id] = replace(stalled, discarded=len(run.running), strict=strict)
        return 1

    def _finish_all(self) -> None:
        for run_id in list(self._cancelled):
            if self._denied:
                return
            self._finish(run_id)

    def _finish(self, run_id: int) -> None:
        stalled = self._cancelled[run_id]
        try:
            conclusion, refusal = self._rerun_cancelled(stalled.repository, run_id)
        except GitHubError as err:
            if not err.transient:
                del self._cancelled[run_id]
                self._refused(err, f"{stalled.repository} run {run_id} not re-run")
            return
        if conclusion is not None:
            del self._cancelled[run_id]
            self._report(run_id, stalled, conclusion, refusal)

    def _rerun_cancelled(self, repository: str, run_id: int) -> tuple[str | None, str | None]:
        conclusion = self._control.run_conclusion(repository, run_id)
        if conclusion != "cancelled":
            return conclusion, None
        strict = self._cancelled[run_id].strict
        return conclusion, self._control.rerun_safely(repository, run_id, strict=strict)

    @staticmethod
    def _report(run_id: int, stalled: _Stalled, conclusion: str, refusal: str | None) -> None:
        if refusal is not None:
            log.warning("%s run %s not re-run: %s", stalled.repository, run_id, refusal)
            return
        if conclusion != "cancelled":
            log.info("%s run %s not rerouted: it finished as %s", stalled.repository, run_id, conclusion)
            return
        log.info(
            "rerouted %s run %s: %d jobs waited %.0f s, discarded %d running jobs",
            stalled.repository,
            run_id,
            stalled.jobs,
            stalled.waited,
            stalled.discarded,
        )

    def _refused(self, err: GitHubError, what: str) -> None:
        if err.status != _HTTP_FORBIDDEN:
            log.warning("%s: %s", what, err)
        elif not self._denied:
            self._denied = True
            log.warning("reroute needs the GitHub App permission Actions: Read and write")
