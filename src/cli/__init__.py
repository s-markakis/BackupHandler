"""
src.cli - Command-line interface.

Modules:
  app        argument parsing and command routing (``main``)
  pipeline   one backup run (``backup_operation``)
  scheduler  long-running scheduled mode
  notify     notification fan-out
  status     ``--status`` dashboard
  common     paths and single-instance lock
"""

from .app import main

__all__ = ["main"]
