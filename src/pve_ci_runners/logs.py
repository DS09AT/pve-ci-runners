"""Retention for the logs kept from VMs whose job did not succeed."""

from pathlib import Path

_DAY = 86400


def prune_logs(log_dir: Path, retention_days: int, now: float) -> int:
    """Deletes kept logs older than the retention; returns how many were removed."""
    if not log_dir.is_dir():
        return 0
    removed = 0
    for path in log_dir.glob("*.log"):
        if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime > retention_days * _DAY:
            path.unlink()
            removed += 1
    return removed
