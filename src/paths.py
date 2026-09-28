"""
paths.py - Project Root Resolution

Every runtime file (``config/``, ``Logs/``, ``BackupTimestamp/``,
``snapshots/``, the instance lock) lives under one project root:

  1. ``$BACKUP_HANDLER_HOME`` if set (use this for installed/packaged runs).
  2. The source checkout, when running from a clone (``src/`` sits next to
     ``config/``).
  3. Otherwise the current working directory (e.g. ``WORKDIR /app`` in the
     Docker image, where ``config/`` is copied).
"""

from __future__ import annotations

import os
from pathlib import Path

_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    env = os.environ.get("BACKUP_HANDLER_HOME")
    if env:
        return Path(env).expanduser()
    if (_CHECKOUT_ROOT / "config").is_dir() and (_CHECKOUT_ROOT / "src").is_dir():
        return _CHECKOUT_ROOT
    return Path.cwd()


PROJECT_ROOT = project_root()
CONFIG_DIR = PROJECT_ROOT / "config"
