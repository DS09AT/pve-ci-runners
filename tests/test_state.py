import json

import pytest

from pve_ci_runners.state import CacheLease, VerifiedJob, VmState, decode

PAYLOAD = "0123456789abcdef"
HEAD = "ab" * 20


def test_a_full_state_survives_the_round_trip() -> None:
    state = VmState(
        phase="assigned",
        since=1000,
        profile="default",
        payload=PAYLOAD,
        repository="example-org/app",
        runner="ci-runner-121-abc123",
        job_started=1010,
        cache=CacheLease(
            "example-org__app/ci__build", "rpool/ci-cache/jobs/x-121-1", "rpool/ci-cache/repos/k@g1"
        ),
        verified=VerifiedJob(
            5, "build", ".github/workflows/ci.yml", "main", "push", same_repository=True, head_sha=HEAD
        ),
    )

    assert decode(state.encode(), running=True) == state


@pytest.mark.parametrize(
    "description",
    [
        "",
        "free text written by an operator",
        "[]",
        json.dumps({"format": 2, "phase": "warm", "since": 1, "profile": "default", "payload": PAYLOAD}),
        json.dumps({"format": 1, "phase": "gone", "since": 1, "profile": "default", "payload": PAYLOAD}),
        json.dumps({"format": 1, "phase": "warm", "since": -1, "profile": "default", "payload": PAYLOAD}),
        json.dumps({"format": 1, "phase": "warm", "since": 1, "profile": "Bad Name", "payload": PAYLOAD}),
        json.dumps({
            "format": 1,
            "phase": "warm",
            "since": 1,
            "profile": "default",
            "payload": PAYLOAD,
            "repository": "x",
        }),
        json.dumps({
            "format": 1,
            "phase": "assigned",
            "since": 1,
            "profile": "default",
            "payload": PAYLOAD,
            "cache": {"key": "../../evil", "clone": "a/b", "origin": "a/b@c"},
        }),
    ],
)
def test_anything_malformed_is_not_ours(description: str) -> None:
    assert decode(description, running=True) is None


@pytest.mark.parametrize("head_sha", ["main", HEAD.upper(), HEAD[:39], HEAD + "0", None])
def test_a_verified_job_needs_an_exact_commit(head_sha: str | None) -> None:
    verified = {
        "run_id": 5,
        "job": "build",
        "workflow": "ci.yml",
        "branch": "main",
        "event": "push",
        "same_repository": True,
    }
    if head_sha is not None:
        verified["head_sha"] = head_sha
    base = {"format": 1, "phase": "assigned", "since": 1, "profile": "default", "payload": PAYLOAD}

    assert decode(json.dumps({**base, "verified": verified}), running=True) is None
    assert decode(json.dumps({**base, "verified": {**verified, "head_sha": HEAD}}), running=True) is not None


def test_a_state_too_large_for_a_description_is_refused() -> None:
    state = VmState(phase="warm", since=1, profile="default", payload=PAYLOAD, runner="r" * 9000)

    with pytest.raises(ValueError, match="does not fit"):
        state.encode()


def test_evolve_keeps_everything_else() -> None:
    state = VmState(phase="warm", since=1, profile="default", payload=PAYLOAD)

    assert state.evolve(phase="assigned").profile == "default"
