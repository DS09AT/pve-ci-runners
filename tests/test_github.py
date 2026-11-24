import json

import pytest

from pve_ci_runners.actions import Actions, Runs
from pve_ci_runners.errors import CommandError, GitHubError
from pve_ci_runners.github import AppKeySigner, GitHubClient, Response, UrllibTransport
from pve_ci_runners.proc import Completed


class Transport:
    def __init__(self, answers: dict[tuple[str, str], list[Response]]) -> None:
        self.answers = answers
        self.sent: list[tuple[str, str, dict[str, str]]] = []

    def send(self, method, url, headers, body):
        path = url.removeprefix("https://api.github.com")
        self.sent.append((method, path, headers))
        queue = self.answers[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def ok(payload: object, status: int = 200, etag: str | None = None) -> Response:
    headers = {"ETag": etag} if etag else {}
    return Response(status, headers, json.dumps(payload).encode())


AUTH = {
    ("GET", "/app/installations"): [
        ok([{"id": 7, "account": {"login": "Example-Org"}}, {"id": 8, "account": {"login": "x"}}])
    ],
    ("POST", "/app/installations/7/access_tokens"): [
        ok({"token": "ghs_secret", "expires_at": "2030-01-01T00:00:00+00:00"})
    ],
}


class Signer:
    def sign(self, message: bytes) -> bytes:
        return b"signature"


def client(extra: dict, sleeps: list[float] | None = None) -> tuple[GitHubClient, Transport]:
    transport = Transport({**AUTH, **extra})
    return (
        GitHubClient(
            api_url="https://api.github.com",
            client_id="Iv23liEXAMPLE",
            owner="example-org",
            signer=Signer(),
            transport=transport,
            clock=lambda: 1_800_000_000.0,
            sleep=(sleeps.append if sleeps is not None else lambda _: None),
        ),
        transport,
    )


def test_requests_use_the_installation_token_of_the_configured_owner() -> None:
    api, transport = client({("GET", "/x"): [ok({"a": 1})]})

    assert api.get("/x") == {"a": 1}
    assert transport.sent[0][2]["Authorization"].startswith("Bearer ey")
    assert transport.sent[-1][2]["Authorization"] == "Bearer ghs_secret"
    api.get("/x")
    assert [s[1] for s in transport.sent].count("/app/installations") == 1, "the token is cached"


def test_conditional_requests_reuse_the_last_payload_on_304() -> None:
    api, transport = client({("GET", "/runs"): [ok({"n": 1}, etag='"v1"'), Response(304, {}, b"")]})

    assert api.get("/runs", conditional=True) == {"n": 1}
    assert api.get("/runs", conditional=True) == {"n": 1}
    assert transport.sent[-1][2]["If-None-Match"] == '"v1"'


def test_gets_are_retried_on_server_errors_but_writes_are_not() -> None:
    sleeps: list[float] = []
    api, _ = client(
        {("GET", "/x"): [ok({}, 502), ok({}, 500), ok({"ok": True})], ("POST", "/y"): [ok({}, 502)]}, sleeps
    )

    assert api.get("/x") == {"ok": True}
    assert sleeps == [1.0, 2.0]
    with pytest.raises(GitHubError) as err:
        api.post("/y", {})
    assert err.value.status == 502


def test_a_get_that_keeps_failing_raises_after_the_last_retry() -> None:
    sleeps: list[float] = []
    api, transport = client({("GET", "/x"): [ok({}, 502), ok({}, 503), ok({"message": "down"}, 504)]}, sleeps)

    with pytest.raises(GitHubError, match="answered 504: down") as err:
        api.get("/x")
    assert err.value.transient
    assert sleeps == [1.0, 2.0]
    assert [s[1] for s in transport.sent].count("/x") == 3


def test_client_errors_are_not_retried_and_never_show_the_token() -> None:
    api, transport = client({("GET", "/x?secret=1"): [ok({"message": "Bad credentials"}, 401)]})

    with pytest.raises(GitHubError, match="answered 401: Bad credentials") as err:
        api.get("/x?secret=1")
    assert "ghs_secret" not in str(err.value)
    assert "secret=1" not in str(err.value)
    assert len([s for s in transport.sent if s[1] == "/x?secret=1"]) == 1


def test_an_uninstalled_app_is_reported() -> None:
    transport = Transport({("GET", "/app/installations"): [ok([])]})
    api = GitHubClient("https://api.github.com", "Iv23", "example-org", Signer(), transport)

    with pytest.raises(GitHubError, match="not installed"):
        api.get("/x")


def test_redirects_are_refused_and_never_followed_with_the_token() -> None:
    import http.server
    import threading

    paths: list[str] = []

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/start"
        with pytest.raises(GitHubError, match="redirected") as err:
            UrllibTransport().send("GET", url, {"Authorization": "Bearer ghs_secret"}, None)
    finally:
        server.shutdown()
        server.server_close()

    assert paths == ["/start"], "no second request is sent"
    assert not err.value.transient


class Commands:
    def __init__(self, result: Completed) -> None:
        self.result = result
        self.argv: list[str] = []

    def run(self, argv, *, stdin=None, timeout=300):
        self.argv = list(argv)
        return self.result


def test_openssl_signs_and_the_key_path_is_an_argument_not_a_shell() -> None:
    commands = Commands(Completed(0, "RSA-SHA2-256(stdin)= 0a0b\n", ""))

    assert AppKeySigner(commands, __import__("pathlib").Path("/etc/k.pem")).sign(b"m") == b"\x0a\x0b"
    assert commands.argv == ["openssl", "dgst", "-sha256", "-hex", "-sign", "/etc/k.pem"]

    with pytest.raises(GitHubError, match="could not sign"):
        AppKeySigner(Commands(Completed(1, "", "unable to load key")), __import__("pathlib").Path("/k")).sign(
            b"m"
        )
    assert isinstance(CommandError("x"), Exception)


class Api:
    def __init__(self, gets: dict[str, object], fail: dict[str, GitHubError] | None = None) -> None:
        self.gets = gets
        self.fail = fail or {}
        self.writes: list[tuple[str, str, object]] = []
        self.deleted: list[str] = []
        self.conditional: dict[str, bool] = {}

    def get(self, path, *, conditional=False):
        self.conditional[path] = conditional
        if path in self.fail:
            raise self.fail[path]
        return self.gets[path]

    def post(self, path, body):
        if path in self.fail:
            raise self.fail[path]
        self.writes.append(("POST", path, body))
        return self.gets.get(path, {})

    def patch(self, path, body):
        if path in self.fail:
            raise self.fail[path]
        self.writes.append(("PATCH", path, body))
        return {}

    def delete(self, path):
        self.deleted.append(path)
        if path in self.fail:
            raise self.fail[path]
        self.writes.append(("DELETE", path, None))
        return None


REPOS = "/installation/repositories?per_page=100&page=1"


def repo(name: str, **changes: object) -> dict[str, object]:
    fields: dict[str, object] = {"private": True, "visibility": "private", "archived": False}
    return {"full_name": name, "default_branch": "main", **fields, **changes}


def test_only_private_unarchived_and_allowed_repositories_are_served() -> None:
    api = Api({
        REPOS: {
            "repositories": [
                repo("example-org/app", default_branch="trunk"),
                repo("example-org/site", private=False, visibility="public"),
                repo("example-org/old", archived=True),
                repo("example-org/other"),
                repo("../../evil"),
            ]
        }
    })

    assert Actions(api).repositories() == {"example-org/app": "trunk", "example-org/other": "main"}
    assert Actions(api, allowed=("Example-Org/app",)).repositories() == {"example-org/app": "trunk"}


def test_internal_repositories_and_unknown_visibility_or_default_branch_are_not_served() -> None:
    unknown = repo("example-org/unknown")
    del unknown["visibility"]
    api = Api({
        REPOS: {
            "repositories": [
                repo("example-org/internal", visibility="internal"),
                repo("example-org/public", visibility="public"),
                unknown,
                repo("example-org/nobranch", default_branch=None),
                repo("example-org/empty", default_branch=""),
            ]
        }
    })

    assert Actions(api).repositories() == {}


def test_queued_jobs_and_verification_come_from_the_api() -> None:
    jobs = {
        "jobs": [
            {
                "status": "queued",
                "labels": ["self-hosted", "pve-ci", 5],
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "status": "in_progress",
                "runner_name": "ci-runner-121-abc",
                "name": "build (linux)",
                "labels": [],
            },
        ]
    }
    api = Api({
        "/repos/o/r/actions/runs?status=queued&per_page=50": {"workflow_runs": [{"id": 9}]},
        "/repos/o/r/actions/runs?status=pending&per_page=50": {
            "workflow_runs": [{"id": 9}, {"id": 7, "run_attempt": 1}]
        },
        "/repos/o/r/actions/runs?status=waiting&per_page=50": {"workflow_runs": []},
        "/repos/o/r/actions/runs?status=requested&per_page=50": {"workflow_runs": []},
        "/repos/o/r/actions/runs?status=in_progress&per_page=50": {"workflow_runs": [{"id": 9}]},
        "/repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": jobs,
        "/repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100": {
            "total_count": 1,
            "jobs": [
                {
                    "status": "queued",
                    "labels": ["self-hosted", "pve-ci"],
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        },
        "/repos/o/r/actions/runs/9": {
            "path": ".github/workflows/ci.yml",
            "head_branch": "main",
            "event": "push",
            "head_repository": {"full_name": "o/r"},
            "head_sha": "a" * 40,
        },
        "/repos/o/r/branches/release%2F1.x": {"commit": {"sha": "b" * 40}},
        "/repos/o/r/branches/odd": {"commit": {"sha": "main"}},
        "/repos/o/r/actions/runners?per_page=100": {
            "runners": [{"name": "a", "busy": True}, {"name": "b", "busy": False}]
        },
    })
    actions = Actions(api)

    queued = actions.queued_jobs("o/r")
    assert [sorted(j.labels) for j in queued] == [["pve-ci", "self-hosted"]] * 2, (
        "a job of a pending run is found, and a run listed under two statuses counts once"
    )
    assert [(j.run_id, j.run_unstarted) for j in queued] == [(9, False), (7, True)]
    verified = actions.verify("o/r", 9, "ci-runner-121-abc", running=True)
    assert verified is not None and verified.job == "build" and verified.same_repository
    assert verified.head_sha == "a" * 40
    assert actions.branch_head("o/r", "release/1.x") == "b" * 40
    with pytest.raises(GitHubError, match="unexpected commit"):
        actions.branch_head("o/r", "odd")
    assert actions.verify("o/r", 9, "someone-else", running=False) is None
    assert actions.conclusion("o/r", 9, "ci-runner-121-abc") is None
    assert actions.busy_runners("o/r") == frozenset({"a"})


def test_registration_and_variable_writes() -> None:
    path = "/repos/o/r/actions/runners/generate-jitconfig"
    api = Api(
        {path: {"runner": {"id": 3}, "encoded_jit_config": "abc="}},
        {"/repos/o/r/actions/variables/V": GitHubError("missing", 404)},
    )
    actions = Actions(api)

    assert actions.register("o/r", "n", ("self-hosted",)).encoded_config == "abc="
    actions.set_variable("o/r", "V", '"x"')
    assert api.writes[-1] == ("POST", "/repos/o/r/actions/variables", {"name": "V", "value": '"x"'})

    bad = Api({path: {"runner": {"id": 3}, "encoded_jit_config": "not base64 ;"}})
    with pytest.raises(GitHubError, match="no usable"):
        Actions(bad).register("o/r", "n", ())


def test_a_completed_job_does_not_verify_as_running() -> None:
    api = Api({
        "/repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": {
            "jobs": [{"status": "completed", "conclusion": "success", "runner_name": "ci-runner-121-abc"}]
        },
        "/repos/o/r/actions/runs/9": {"path": "ci.yml", "event": "push", "head_sha": "a" * 40},
    })
    actions = Actions(api)

    assert actions.verify("o/r", 9, "ci-runner-121-abc", running=True) is None
    assert actions.verify("o/r", 9, "ci-runner-121-abc", running=False) is not None


LOOKUP = "/repos/o/r/actions/runners?name=ci-runner-121-abc"


def test_only_a_runner_of_exactly_the_given_name_is_deleted() -> None:
    found = {"runners": [{"id": 4, "name": "ci-runner-121-abcd"}, {"id": 5, "name": "ci-runner-121-abc"}]}
    api = Api({LOOKUP: found}, {"/repos/o/r/actions/runners/5": GitHubError("gone", 404)})
    Actions(api).remove_runner("o/r", "ci-runner-121-abc")
    assert api.deleted == ["/repos/o/r/actions/runners/5"]

    other = Api({LOOKUP: {"runners": [{"id": 4, "name": "someone-else"}]}})
    Actions(other).remove_runner("o/r", "ci-runner-121-abc")
    assert other.deleted == []


def test_an_unknown_runner_lookup_deletes_nothing() -> None:
    api = Api({}, {LOOKUP: GitHubError("not found", 404)})
    Actions(api).remove_runner("o/r", "ci-runner-121-abc")
    assert api.deleted == []

    failing = Api({}, {LOOKUP: GitHubError("forbidden", 403)})
    with pytest.raises(GitHubError, match="forbidden"):
        Actions(failing).remove_runner("o/r", "ci-runner-121-abc")


def runs_api(jobs: dict[str, object], *, attempt: object = 1) -> Api:
    gets: dict[str, object] = {
        f"/repos/o/r/actions/runs?status={s}&per_page=50": {"workflow_runs": []}
        for s in ("pending", "waiting", "requested", "in_progress")
    }
    gets["/repos/o/r/actions/runs?status=queued&per_page=50"] = {
        "workflow_runs": [{"id": 3, "run_attempt": attempt}]
    }
    gets["/repos/o/r/actions/runs/3/jobs?filter=latest&per_page=100"] = jobs
    gets["/repos/o/r/actions/runs/3"] = {
        "status": "completed",
        "conclusion": "cancelled",
        "workflow_id": 11,
        "head_branch": "release/1.x",
        "created_at": "2026-01-01T00:00:00Z",
    }
    gets[NEWER] = {"workflow_runs": [{"id": 3, "created_at": "2026-01-01T00:00:00Z"}]}
    return Api(gets)


NEWER = "/repos/o/r/actions/workflows/11/runs?branch=release%2F1.x&per_page=5"
JOBS = "/repos/o/r/actions/runs/3/jobs?filter=latest&per_page=100"


QUEUED = {"status": "queued", "labels": [], "created_at": "2026-01-01T00:00:00Z"}


@pytest.mark.parametrize(
    ("other", "unstarted"),
    [
        ({"status": "completed", "conclusion": "skipped"}, True),
        ({"status": "waiting"}, True),
        ({"status": "completed", "conclusion": "success"}, False),
        ({"status": "in_progress", "runner_name": "ci-runner-121-abc"}, False),
        ({"status": "waiting", "runner_name": "ci-runner-121-abc"}, False),
    ],
)
def test_a_run_counts_as_unstarted_only_while_no_job_did_work(other: dict, unstarted: bool) -> None:
    api = runs_api({"total_count": 2, "jobs": [QUEUED, other]})

    assert Actions(api).queued_jobs("o/r")[0].run_unstarted is unstarted
    assert Runs(api).cancel_unstarted("o/r", 3) is unstarted
    assert api.conditional[JOBS] is False, "the check right before the cancellation is never served stale"
    assert api.writes == ([("POST", "/repos/o/r/actions/runs/3/cancel", {})] if unstarted else [])


@pytest.mark.parametrize(("total", "attempt"), [(3, 1), (None, 1), (True, 1), (2, 2), (2, None)], ids=str)
def test_a_run_with_unlisted_jobs_or_a_later_attempt_is_never_unstarted(
    total: object, attempt: object
) -> None:
    api = runs_api({"total_count": total, "jobs": [QUEUED, QUEUED]}, attempt=attempt)

    assert Actions(api).queued_jobs("o/r")[0].run_unstarted is False
    if attempt == 1:
        assert Runs(api).cancel_unstarted("o/r", 3) is False and api.writes == []


@pytest.mark.parametrize(
    ("job", "rerun"),
    [
        (
            {
                "status": "completed",
                "conclusion": "cancelled",
                "started_at": "2026-01-01T00:00:00Z",
                "steps": [],
            },
            True,
        ),
        ({"status": "completed", "conclusion": "cancelled", "runner_name": "ci-runner-121-abc"}, False),
        (
            {"status": "completed", "started_at": "2026-01-01T00:00:00Z", "steps": [{"name": "Set up job"}]},
            False,
        ),
    ],
)
def test_a_cancelled_run_is_re_run_only_if_none_of_its_jobs_ran(job: dict, rerun: bool) -> None:
    api = runs_api({"total_count": 1, "jobs": [job]})
    runs = Runs(api)

    assert runs.run_conclusion("o/r", 3) == "cancelled"
    assert (runs.rerun_safely("o/r", 3) is None) is rerun
    assert api.writes == ([("POST", "/repos/o/r/actions/runs/3/rerun", {})] if rerun else [])
    assert api.conditional[JOBS] is False


def test_a_run_with_more_jobs_than_listed_is_not_re_run() -> None:
    untouched = {"status": "completed", "conclusion": "cancelled", "steps": []}
    api = runs_api({"total_count": 101, "jobs": [untouched] * 100})

    assert Runs(api).rerun_safely("o/r", 3) == "GitHub does not list all of its jobs"
    assert api.writes == []


def test_a_run_a_newer_run_of_its_workflow_and_branch_superseded_is_not_re_run() -> None:
    api = runs_api({"total_count": 0, "jobs": []})
    api.gets[NEWER] = {
        "workflow_runs": [
            {"id": 8, "created_at": "2026-01-01T00:05:00Z"},
            {"id": 3, "created_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "created_at": "2025-12-31T23:00:00Z"},
        ]
    }

    assert Runs(api).rerun_safely("o/r", 3) == "run 8 superseded it"
    assert api.writes == []


def test_a_run_superseded_within_the_same_second_is_not_re_run() -> None:
    api = runs_api({"total_count": 0, "jobs": []})
    api.gets[NEWER] = {
        "workflow_runs": [
            {"id": 4, "created_at": "2026-01-01T00:00:00Z"},
            {"id": 3, "created_at": "2026-01-01T00:00:00Z"},
        ]
    }

    assert Runs(api).rerun_safely("o/r", 3) == "run 4 superseded it"
    assert api.writes == []


def test_a_run_without_a_branch_is_never_re_run() -> None:
    api = runs_api({"total_count": 0, "jobs": []})
    api.gets["/repos/o/r/actions/runs/3"] = {"workflow_id": 11, "head_branch": None}

    with pytest.raises(GitHubError, match="without a branch"):
        Runs(api).rerun_safely("o/r", 3)
    assert api.writes == []


def test_the_conclusion_of_an_unfinished_run_is_unknown() -> None:
    api = Api({
        "/repos/o/r/actions/runs/4": {"status": "in_progress", "conclusion": None},
        "/repos/o/r/actions/runs/5": {"status": "completed", "conclusion": None},
    })

    assert [Runs(api).run_conclusion("o/r", r) for r in (4, 5)] == [None, "unknown"]
