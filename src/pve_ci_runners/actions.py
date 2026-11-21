"""The GitHub Actions operations the controller needs, with every response checked at this boundary."""

import datetime as dt
import re
import urllib.parse
from dataclasses import dataclass
from typing import Protocol, cast

from pve_ci_runners.config import REPOSITORY
from pve_ci_runners.errors import GitHubError
from pve_ci_runners.state import COMMIT, VerifiedJob

_PAGE = 100
_RUNS_PAGE = 50
_HTTP_NOT_FOUND = 404


class Api(Protocol):
    """The JSON verbs of an authenticated GitHub client."""

    def get(self, path: str, *, conditional: bool = False) -> object:
        """A GET."""
        ...

    def post(self, path: str, body: object) -> object:
        """A POST."""
        ...

    def patch(self, path: str, body: object) -> object:
        """A PATCH."""
        ...

    def delete(self, path: str) -> object:
        """A DELETE."""
        ...


@dataclass(frozen=True)
class QueuedJob:
    """A job waiting for a runner."""

    labels: frozenset[str]
    created_at: float


@dataclass(frozen=True)
class Registration:
    """A single-use runner registration."""

    encoded_config: str


class Actions:
    """Repositories, queued jobs and runner registrations of one App installation."""

    def __init__(self, api: Api, *, allowed: tuple[str, ...] = ()) -> None:
        self._api = api
        self._allowed = {r.lower() for r in allowed}

    def repositories(self) -> dict[str, str]:
        """Private, non-archived repositories of the installation, with their default branches.

        Public and internal repositories are never served: anyone, or every member of the enterprise, can
        open a pull request against them, and its code would run on this host. A repository without a
        known default branch is skipped, because no run of it could be proven trusted.
        """
        found: dict[str, str] = {}
        page = 1
        while True:
            answer = _obj(self._api.get(f"/installation/repositories?per_page={_PAGE}&page={page}"))
            batch = _objs(answer.get("repositories"))
            found |= {
                _str(repo.get("full_name")): _str(repo.get("default_branch"))
                for repo in batch
                if self._served(repo)
            }
            if len(batch) < _PAGE:
                return found
            page += 1

    def _served(self, repo: dict[str, object]) -> bool:
        name = _str(repo.get("full_name"))
        if repo.get("visibility") != "private" or repo.get("private") is not True:
            return False
        if repo.get("archived") is not False or not _str(repo.get("default_branch")):
            return False
        if re.fullmatch(REPOSITORY, name) is None:
            return False
        return not self._allowed or name.lower() in self._allowed

    def queued_jobs(self, repository: str) -> list[QueuedJob]:
        """Every queued job of the repository's queued or running workflow runs."""
        jobs: list[QueuedJob] = []
        for status in ("queued", "in_progress"):
            runs = _obj(
                self._api.get(
                    f"/repos/{repository}/actions/runs?status={status}&per_page={_RUNS_PAGE}",
                    conditional=True,
                )
            )
            for run in _objs(runs.get("workflow_runs")):
                jobs += self._queued_in(repository, _int(run.get("id")))
        return jobs

    def _queued_in(self, repository: str, run_id: int) -> list[QueuedJob]:
        jobs: list[QueuedJob] = []
        for job in self._jobs(repository, run_id):
            if job.get("status") == "queued":
                labels = frozenset(label for label in _list(job.get("labels")) if isinstance(label, str))
                jobs.append(QueuedJob(labels, _timestamp(job.get("created_at"))))
        return jobs

    def busy_runners(self, repository: str) -> frozenset[str]:
        """Names of the repository's runners that are running a job right now."""
        answer = _obj(self._api.get(f"/repos/{repository}/actions/runners?per_page={_PAGE}"))
        return frozenset(_str(r.get("name")) for r in _objs(answer.get("runners")) if r.get("busy") is True)

    def register(self, repository: str, name: str, labels: tuple[str, ...]) -> Registration:
        """A just-in-time configuration for one ephemeral runner."""
        answer = _obj(
            self._api.post(
                f"/repos/{repository}/actions/runners/generate-jitconfig",
                {"name": name, "runner_group_id": 1, "labels": list(labels), "work_folder": "_work"},
            )
        )
        config = _str(answer.get("encoded_jit_config"))
        if not config or not re.fullmatch(r"[A-Za-z0-9+/=]+", config):
            raise GitHubError("GitHub returned no usable runner configuration")
        return Registration(config)

    def verify(self, repository: str, run_id: int, runner: str, *, running: bool) -> VerifiedJob | None:
        """The job this runner serves, or None when GitHub does not show it on that run.

        With `running`, only a job in progress counts: a finished job of an earlier VM in the same slot
        carries a runner name of the same pattern and must not be mistaken for the current one.
        """
        mine = [j for j in self._jobs(repository, run_id) if j.get("runner_name") == runner]
        if not mine or (running and mine[0].get("status") != "in_progress"):
            return None
        run = _obj(self._api.get(f"/repos/{repository}/actions/runs/{run_id}"))
        head = run.get("head_repository")
        head_name = _str(_obj(head).get("full_name")) if isinstance(head, dict) else ""
        return VerifiedJob(
            run_id=run_id,
            job=_str(mine[0].get("name")).split(" (")[0][:200] or "job",
            workflow=_str(run.get("path"))[:250] or "workflow",
            branch=_str(run.get("head_branch"))[:250],
            event=_str(run.get("event"))[:50],
            same_repository=head_name.lower() == repository.lower(),
            head_sha=_commit(run.get("head_sha")),
        )

    def branch_head(self, repository: str, branch: str) -> str:
        """The commit a branch points to right now."""
        answer = _obj(self._api.get(f"/repos/{repository}/branches/{urllib.parse.quote(branch, safe='')}"))
        return _commit(_obj(answer.get("commit")).get("sha"))

    def conclusion(self, repository: str, run_id: int, runner: str) -> str | None:
        """The conclusion of the runner's job, or None while GitHub has not recorded it."""
        mine = [j for j in self._jobs(repository, run_id) if j.get("runner_name") == runner]
        if not mine or mine[0].get("status") != "completed":
            return None
        return _str(mine[0].get("conclusion")) or None

    def remove_runner(self, repository: str, name: str) -> None:
        """Deletes the registration of exactly this name; one that is already gone is not an error."""
        try:
            answer = _obj(
                self._api.get(f"/repos/{repository}/actions/runners?name={urllib.parse.quote(name, safe='')}")
            )
            for runner in _objs(answer.get("runners")):
                if runner.get("name") == name:
                    self._api.delete(f"/repos/{repository}/actions/runners/{_int(runner.get('id'))}")
        except GitHubError as err:
            if err.status != _HTTP_NOT_FOUND:
                raise

    def set_variable(self, repository: str, name: str, value: str) -> None:
        """Creates or updates a repository Actions variable."""
        try:
            self._api.patch(f"/repos/{repository}/actions/variables/{name}", {"name": name, "value": value})
        except GitHubError as err:
            if err.status != _HTTP_NOT_FOUND:
                raise
            self._api.post(f"/repos/{repository}/actions/variables", {"name": name, "value": value})

    def _jobs(self, repository: str, run_id: int) -> list[dict[str, object]]:
        answer = _obj(
            self._api.get(
                f"/repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page={_PAGE}",
                conditional=True,
            )
        )
        return _objs(answer.get("jobs"))


def _obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GitHubError("GitHub returned an unexpected response")
    return cast("dict[str, object]", value)


def _objs(value: object) -> list[dict[str, object]]:
    return [cast("dict[str, object]", v) for v in _list(value) if isinstance(v, dict)]


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GitHubError("GitHub returned an unexpected identifier")
    return value


def _commit(value: object) -> str:
    if re.fullmatch(COMMIT, _str(value)) is None:
        raise GitHubError("GitHub returned an unexpected commit")
    return _str(value)


def _timestamp(value: object) -> float:
    try:
        return dt.datetime.fromisoformat(_str(value)).timestamp()
    except ValueError:
        raise GitHubError("GitHub returned an unexpected timestamp") from None
