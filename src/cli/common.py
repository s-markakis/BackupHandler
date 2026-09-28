"""
common.py - Shared CLI paths and single-instance locking.
"""

import atexit
import contextlib
import os
import sys
from pathlib import Path

from ..paths import PROJECT_ROOT

CONFIG_PATH = str(PROJECT_ROOT / "config" / "config.ini")
LOG_PATH = str(PROJECT_ROOT / "Logs" / "application.log")
LOCK_FILE = PROJECT_ROOT / ".backup-handler.lock"


# ─── Instance Locking ───────────────────────────────────────────────────────


def _proc_looks_like_backup_handler(pid: int) -> bool:
    """
    Confirm that a PID corresponds to a backup-handler process.

    PIDs are recycled by the OS. A stale lock file can point at an
    unrelated process that happens to share the old PID. Cross-check
    ``/proc/<pid>/comm`` and ``/proc/<pid>/cmdline`` before trusting it.
    Returns True only when the process's identifiers reference python or
    the backup-handler entry point.
    """
    comm = Path(f"/proc/{pid}/comm")
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        comm_value = comm.read_text().strip().lower() if comm.exists() else ""
        cmdline_value = cmdline.read_text().replace("\x00", " ").lower() if cmdline.exists() else ""
    except OSError:
        return False
    hints = ("python", "backup-handler", "main.py", "src.cli")
    return any(h in comm_value or h in cmdline_value for h in hints)


def _acquire_lock(logger):
    """
    Acquire a PID lock file to prevent duplicate scheduled instances.

    Checks if an existing lock file references a still-running backup-handler
    process. Stale lock files (from crashed instances or recycled PIDs) are
    automatically cleaned up. Registers ``_release_lock`` via ``atexit``.
    """
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            os.kill(old_pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            logger.warning("Removing stale lock file (PID in file no longer running).")
        else:
            if _proc_looks_like_backup_handler(old_pid):
                logger.error(
                    f"Another backup-handler instance is already running (PID {old_pid}). "
                    f"Remove {LOCK_FILE} if this is incorrect."
                )
                sys.exit(1)
            logger.warning(
                f"Lock file references PID {old_pid} but that process is not "
                f"backup-handler (likely recycled). Reclaiming the lock."
            )

    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock():
    """Remove the PID lock file on exit."""
    with contextlib.suppress(OSError):
        LOCK_FILE.unlink(missing_ok=True)
