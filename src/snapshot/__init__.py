"""
snapshot.py - System State Snapshot & Restore

Captures a complete snapshot of installed packages, configurations, and system
state across Ubuntu/Linux and Windows. Generates restore scripts that can
rebuild the system after a fresh OS install.

Collectors are organized by category and gracefully skip unavailable tools.
"""

from .create import create_snapshot
from .diff import diff_snapshots
from .restore_script import generate_restore_script

__all__ = ["create_snapshot", "diff_snapshots", "generate_restore_script"]
