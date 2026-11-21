"""Flag files for maintenance: one holds back new work, one reserves memory for a template build."""

import fcntl
from collections.abc import Generator
from contextlib import contextmanager
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


def reserved_mb(state_dir: Path) -> int:
    """Memory a template build holds; the controller keeps it free in addition to the host reserve."""
    try:
        return max(0, int((state_dir / "reservation").read_text(encoding="utf-8").strip()))
    except FileNotFoundError:
        return 0
    except ValueError:
        # An unreadable or empty reservation must not shrink the reserve to nothing.
        return 2**31


def reserve(state_dir: Path, memory_mb: int) -> None:
    """Reserves memory for a template build."""
    state_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    # Replaced, never truncated in place: the controller must not read a half-written reservation.
    partial = state_dir / "reservation.part"
    partial.write_text(str(memory_mb), encoding="utf-8")
    partial.replace(state_dir / "reservation")


def release(state_dir: Path) -> None:
    """Gives a template build's memory back to the runners."""
    (state_dir / "reservation").unlink(missing_ok=True)


@contextmanager
def memory_lock(state_dir: Path) -> Generator[None]:
    """Serialises a memory check with the VM start it allows, across the controller and a build."""
    state_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    with (state_dir / "memory.lock").open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
