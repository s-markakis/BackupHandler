"""
_common.py - Shared helpers: command execution, quoting, private file writes.
"""

import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

# ─── Utility ────────────────────────────────────────────────────────────────


def _run(cmd, timeout=30, shell=False):
    """Run a command and return stdout lines, or empty list on failure.

    shell is opt-in and only used by callers passing a trusted, fixed command
    string (e.g., 'lsblk | awk ...'); never user-controlled input.
    """
    try:
        result = subprocess.run(  # nosec B602
            cmd, capture_output=True, text=True, timeout=timeout, shell=shell
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _run_single(cmd, timeout=30, shell=False):
    """Run a command and return stdout as a single string, or None."""
    lines = _run(cmd, timeout=timeout, shell=shell)  # nosec B604
    return "\n".join(lines) if lines else None


def _write_private(path, text, mode=0o600):
    """Write ``text`` to ``path`` readable only by the owner.

    Snapshots and generated restore scripts carry dotfiles, crontabs and
    environment variables, which routinely contain tokens.
    """
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    path.chmod(mode)  # O_CREAT mode is ignored when the file already exists


def _q(value):
    """Shell-quote a value taken from a snapshot for use in the bash restore script."""
    return shlex.quote(str(value))


def _ps_q(value):
    """Quote a value as a PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_relpath(rel_path):
    """True if ``rel_path`` is relative and stays inside its base directory."""
    p = Path(rel_path)
    return bool(rel_path) and not p.is_absolute() and ".." not in p.parts


def _which(name):
    """Check if a command is available."""
    return shutil.which(name) is not None


def _is_linux():
    return platform.system() == "Linux"


def _is_windows():
    return platform.system() == "Windows"


def _home():
    return Path.home()
