"""The error types callers act on; each names the boundary that failed."""


class PveCiError(Exception):
    """Base class for every expected failure of the controller."""


class ConfigError(PveCiError):
    """The configuration file is missing, malformed or inconsistent."""


class CommandError(PveCiError):
    """A host command exited non-zero or timed out."""


class GitHubError(PveCiError):
    """The GitHub API refused a request or could not be reached."""

    def __init__(self, message: str, status: int | None = None, *, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient or (status is not None and status >= 500)  # noqa: PLR2004 - HTTP server errors


class GuestError(PveCiError):
    """A command inside a VM failed, timed out or reported something invalid."""

    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
