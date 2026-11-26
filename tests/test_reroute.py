import dataclasses
import logging

import pytest

from pve_ci_runners.actions import QueuedJob, RunSummary
from pve_ci_runners.config import parse
from pve_ci_runners.errors import GitHubError
from pve_ci_runners.reroute import PER_CYCLE, Reroute
from tests.conftest import config_data

SELF = frozenset({"self-hosted", "pve-ci"})
REPO = "example-org/app"
NOW = 10_000.0
UNSTARTED = RunSummary(complete=True, event="push", unstarted=True)
STARTED = RunSummary(complete=True, event="push", unstarted=False, running=(NOW - 10,))


def pull_request(*running: float) -> RunSummary:
    return RunSummary(complete=True, event="pull_request", unstarted=False, running=tuple(running))


class Control:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.conclusions: dict[int, str | None] = {}
        self.fail: dict[str, GitHubError] = {}
        self.fresh: dict[int, RunSummary] = {}
        self.refusals: dict[int, str] = {}
        self.strict: dict[int, bool] = {}

    def summary(self, repository: str, run_id: int) -> RunSummary:
        if "summary" in self.fail:
            raise self.fail["summary"]
        return self.fresh.get(run_id, UNSTARTED)

    def cancel(self, repository: str, run_id: int) -> None:
        self._call("cancel", run_id)
        self.conclusions.setdefault(run_id, "cancelled")

    def rerun_safely(self, repository: str, run_id: int, *, strict: bool) -> str | None:
        self.strict[run_id] = strict
        if run_id in self.refusals:
            return self.refusals[run_id]
        self._call("rerun", run_id)
        return None

    def run_conclusion(self, repository: str, run_id: int) -> str | None:
        if "conclusion" in self.fail:
            raise self.fail["conclusion"]
        return self.conclusions.get(run_id)

    def _call(self, verb: str, run_id: int) -> None:
        if verb in self.fail:
            raise self.fail[verb]
        self.calls.append((verb, run_id))


def job(run_id: int, waited: float = 300, *, labels=SELF, run: RunSummary | None = UNSTARTED) -> QueuedJob:
    return QueuedJob(frozenset(labels), NOW - waited, run_id, run)


def reroute(control: Control, after: int = 120, discard: int = 300, clock: float = NOW) -> Reroute:
    overflow = parse(config_data()).overflow
    assert overflow is not None
    changed = dataclasses.replace(overflow, reroute_after_seconds=after, reroute_discard_seconds=discard)
    return Reroute(changed, control, clock=lambda: clock)


def test_a_stalled_run_is_cancelled_and_re_run_on_a_later_cycle(caplog) -> None:
    caplog.set_level(logging.INFO)
    control = Control()
    rr = reroute(control)

    rr.update({REPO: [job(7, 200), job(7, 300)]}, NOW)
    assert control.calls == [("cancel", 7)]

    rr.update({}, NOW + 15)
    assert control.calls == [("cancel", 7), ("rerun", 7)]
    assert f"rerouted {REPO} run 7: 2 jobs waited 300 s, discarded 0 running jobs" in caplog.text
    assert control.strict == {7: True}


def test_the_re_run_waits_until_github_recorded_the_cancellation() -> None:
    control = Control()
    control.conclusions[7] = None
    rr = reroute(control)

    rr.update({REPO: [job(7)]}, NOW)
    rr.update({}, NOW + 15)
    assert control.calls == [("cancel", 7)]

    control.conclusions[7] = "cancelled"
    rr.update({}, NOW + 30)
    assert control.calls == [("cancel", 7), ("rerun", 7)]


def test_a_run_that_finished_otherwise_is_not_re_run(caplog) -> None:
    caplog.set_level(logging.INFO)
    control = Control()
    control.conclusions[7] = "success"
    rr = reroute(control)

    rr.update({REPO: [job(7)]}, NOW)
    rr.update({}, NOW + 15)
    rr.update({}, NOW + 30)

    assert control.calls == [("cancel", 7)]
    assert "finished as success" in caplog.text


@pytest.mark.parametrize(
    ("queued", "reason"),
    [
        (job(7, 119), "younger than the threshold"),
        (job(7, run=STARTED), "a job of a push run has started"),
        (job(7, run=None), "nothing known about the run"),
        (job(7, run=RunSummary(complete=False, event="push", unstarted=True)), "a later attempt"),
        (job(7, run=pull_request(NOW - 301)), "a pull request job ran longer than the discard bound"),
        (job(7, run=pull_request(NOW - 10, 0.0)), "a running job without a start time"),
        (job(7, labels={"ubuntu-latest"}), "already bound to the hosted runner"),
    ],
)
def test_runs_that_must_not_be_cancelled_are_left_alone(queued: QueuedJob, reason: str) -> None:
    control = Control()

    reroute(control).update({REPO: [queued]}, NOW)

    assert control.calls == [], reason


def test_the_threshold_itself_reroutes_and_labels_match_without_case() -> None:
    control = Control()

    reroute(control).update({REPO: [job(7, 120, labels={"Self-Hosted", "PVE-CI"})]}, NOW)

    assert control.calls == [("cancel", 7)]


def test_each_run_is_rerouted_once() -> None:
    control = Control()
    rr = reroute(control)

    rr.update({REPO: [job(7)]}, NOW)
    rr.update({REPO: [job(7)]}, NOW + 15)
    rr.update({REPO: [job(7)]}, NOW + 30)

    assert control.calls == [("cancel", 7), ("rerun", 7)]


def test_at_most_a_few_runs_are_cancelled_per_cycle() -> None:
    control = Control()
    rr = reroute(control)
    queued = {REPO: [job(run) for run in range(1, PER_CYCLE + 3)]}

    rr.update(queued, NOW)
    assert [c for c in control.calls if c[0] == "cancel"] == [("cancel", r) for r in range(1, PER_CYCLE + 1)]

    rr.update(queued, NOW + 15)
    assert len([c for c in control.calls if c[0] == "cancel"]) == PER_CYCLE + 2


@pytest.mark.parametrize("verb", ["cancel", "rerun"])
def test_a_missing_permission_warns_once_and_stops_rerouting(verb: str, caplog) -> None:
    control = Control()
    control.fail[verb] = GitHubError("Resource not accessible by integration", 403)
    rr = reroute(control)

    for cycle in range(4):
        rr.update({REPO: [job(1), job(2)], "example-org/other": [job(3)]}, NOW + 15 * cycle)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "reroute needs the GitHub App permission Actions: Read and write"
    ]
    assert ("rerun", 1) not in control.calls
    assert len(control.calls) == (0 if verb == "cancel" else 3)


def test_a_refused_cancel_is_logged_and_not_retried(caplog) -> None:
    control = Control()
    control.fail["cancel"] = GitHubError("conflict", 409)
    rr = reroute(control)

    rr.update({REPO: [job(7)]}, NOW)
    del control.fail["cancel"]
    rr.update({REPO: [job(7)]}, NOW + 15)

    assert control.calls == []
    assert "run 7 not cancelled" in caplog.text


def test_an_unknown_outcome_is_asked_again_only_after_a_transient_error(caplog) -> None:
    control = Control()
    rr = reroute(control)
    rr.update({REPO: [job(7), job(8)]}, NOW)

    control.fail["conclusion"] = GitHubError("unavailable", 502)
    rr.update({}, NOW + 15)
    control.fail["conclusion"] = GitHubError("gone", 404)
    rr.update({}, NOW + 30)
    del control.fail["conclusion"]
    rr.update({}, NOW + 45)

    assert control.calls == [("cancel", 7), ("cancel", 8)]
    assert "run 7 not re-run" in caplog.text and "run 8 not re-run" in caplog.text


def test_a_run_whose_job_started_meanwhile_is_not_cancelled_and_does_not_count(caplog) -> None:
    caplog.set_level(logging.INFO)
    control = Control()
    control.fresh = dict.fromkeys(range(1, PER_CYCLE + 1), STARTED)
    rr = reroute(control)
    queued = {REPO: [job(run) for run in range(1, PER_CYCLE + 2)]}

    rr.update(queued, NOW)
    rr.update(queued, NOW + 15)

    assert control.calls == [("cancel", PER_CYCLE + 1), ("rerun", PER_CYCLE + 1)]
    assert f"{REPO} run 1 not rerouted: it no longer qualifies" in caplog.text


def test_a_pull_request_with_young_running_jobs_is_rerouted_without_them(caplog) -> None:
    caplog.set_level(logging.INFO)
    control = Control()
    run = pull_request(NOW - 40, NOW - 30)
    control.fresh[7] = pull_request(NOW - 40, NOW - 30, NOW - 5)
    rr = reroute(control)

    rr.update({REPO: [job(7, run=run)] * 11}, NOW)
    rr.update({}, NOW + 15)

    assert control.calls == [("cancel", 7), ("rerun", 7)]
    assert control.strict == {7: False}, "a job that ran does not stop the re-run of a pull request"
    assert f"rerouted {REPO} run 7: 11 jobs waited 300 s, discarded 3 running jobs" in caplog.text


def test_the_check_right_before_the_cancellation_applies_the_discard_bound_again() -> None:
    control = Control()
    control.fresh[7] = pull_request(NOW - 301)

    reroute(control).update({REPO: [job(7, run=pull_request(NOW - 200))]}, NOW)

    assert control.calls == []


def test_the_check_right_before_the_cancellation_measures_against_the_clock_not_the_cycle_start() -> None:
    control = Control()
    control.fresh[7] = pull_request(NOW - 250)

    reroute(control, clock=NOW + 60).update({REPO: [job(7, run=pull_request(NOW - 250))]}, NOW)

    assert control.calls == [], "the job has run 310 s by the time of the cancellation"


def test_a_discard_bound_of_zero_reroutes_only_unstarted_runs() -> None:
    control = Control()
    rr = reroute(control, discard=0)

    rr.update({REPO: [job(7, run=pull_request(NOW - 5)), job(8, run=pull_request()), job(9)]}, NOW)

    assert control.calls == [("cancel", 9)]


def test_a_failed_check_before_the_cancellation_cancels_nothing(caplog) -> None:
    control = Control()
    control.fail["summary"] = GitHubError("unavailable", 502)

    reroute(control).update({REPO: [job(7)]}, NOW)

    assert control.calls == [] and "run 7 not cancelled" in caplog.text


def test_a_cancelled_run_in_which_a_job_ran_is_not_re_run(caplog) -> None:
    control = Control()
    control.refusals[7] = "a job of it ran after all"
    rr = reroute(control)

    rr.update({REPO: [job(7)]}, NOW)
    rr.update({}, NOW + 15)
    rr.update({}, NOW + 30)

    assert control.calls == [("cancel", 7)]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"{REPO} run 7 not re-run: a job of it ran after all"]


def test_a_superseded_run_is_not_re_run(caplog) -> None:
    control = Control()
    control.refusals[7] = "run 9 superseded it"
    rr = reroute(control)

    rr.update({REPO: [job(7)]}, NOW)
    rr.update({}, NOW + 15)
    rr.update({}, NOW + 30)

    assert control.calls == [("cancel", 7)]
    assert [r.getMessage() for r in caplog.records] == [f"{REPO} run 7 not re-run: run 9 superseded it"]


def test_zero_turns_rerouting_off() -> None:
    control = Control()

    reroute(control, after=0).update({REPO: [job(7, 10_000)]}, NOW)

    assert control.calls == []
