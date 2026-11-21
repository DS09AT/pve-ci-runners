"""The only module that starts host processes.

Every command is an argument list; nothing on the host is ever run through a shell, so no value from a
configuration file, the GitHub API or a guest can change what is executed.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pve_ci_runners.errors import CommandError

_ERROR_TAIL = 300


@dataclass(frozen=True)
class Completed:
    """The outcome of one host command."""

    returncode: int
    stdout: str
    stderr: str


class Commands(Protocol):
    """Runs host commands; the controller and its adapters depend only on this."""

    def run(self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 300) -> Completed:
        """Runs a command and returns its outcome without judging it."""
        ...


class HostCommands:
    """Runs commands on this host with captured output and a hard timeout."""

    def run(  # noqa: PLR6301 - an instance satisfies the Commands protocol and is swapped for fakes in tests
        self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 300
    ) -> Completed:
        """Runs a command and returns its outcome without judging it."""
        try:
            done = subprocess.run(
                list(argv), input=stdin, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as err:
            raise CommandError(f"{_name(argv)} timed out after {timeout:.0f} s") from err
        except OSError as err:
            raise CommandError(f"{_name(argv)} could not start: {err.strerror}") from err
        return Completed(done.returncode, done.stdout, done.stderr)


def output(commands: Commands, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 300) -> str:
    """Runs a command that must succeed and returns its standard output."""
    done = commands.run(argv, stdin=stdin, timeout=timeout)
    if done.returncode != 0:
        raise CommandError(f"{_name(argv)} exited {done.returncode}: {done.stderr.strip()[-_ERROR_TAIL:]}")
    return done.stdout


def succeeds(commands: Commands, argv: Sequence[str], *, timeout: float = 300) -> bool:
    """Runs a command whose exit status is the answer."""
    return commands.run(argv, timeout=timeout).returncode == 0


def _name(argv: Sequence[str]) -> str:
    # Only the command and its subcommand: later arguments may carry configuration values.
    return " ".join(argv[:2])
