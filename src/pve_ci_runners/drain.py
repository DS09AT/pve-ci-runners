"""A flag file that stops new work for maintenance without stopping running jobs."""

from pathlib import Path


def flag(state_dir: Path) -> Path:
    """The path of the drain flag."""
    return state_dir / "drain"


def draining(state_dir: Path) -> bool:
    """Whether new jobs are held back."""
    return flag(state_dir).exists()


def drain(state_dir: Path) -> None:
    """Holds back new jobs; running jobs finish normally."""
    state_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    flag(state_dir).touch(mode=0o644)


def resume(state_dir: Path) -> None:
    """Takes new jobs again."""
    flag(state_dir).unlink(missing_ok=True)
