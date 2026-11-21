"""When a job's cache may become the repository's cache for later jobs.

A cache is shared by every later job of the same workflow job, including jobs from the default branch,
so only a run that the default branch itself produced may write it: anything else could plant files that
a trusted build later executes.
"""

from pve_ci_runners.state import VerifiedJob

TRUSTED_EVENTS = frozenset({"push", "workflow_dispatch", "schedule"})


def may_write_back(
    verified: VerifiedJob, *, conclusion: str | None, default_branch: str, default_head: str
) -> bool:
    """A successful run of a trusted event on the default branch's current head in the repository itself.

    A tag may carry the default branch's name, so the branch name alone does not prove the ref; the
    commit the branch points to does.
    """
    return (
        conclusion == "success"
        and verified.branch == default_branch
        and verified.head_sha == default_head
        and verified.event in TRUSTED_EVENTS
        and verified.same_repository
    )


def worth_keeping(written_bytes: int, *, min_write_mb: int) -> bool:
    """A job that left its cache almost untouched has nothing worth replacing the old one with."""
    return written_bytes >= min_write_mb * 2**20
