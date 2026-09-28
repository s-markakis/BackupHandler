"""
main.py - Backup Handler CLI Entry Point (source-checkout shim)

Central entry point that coordinates the entire backup pipeline:
  1. Parse CLI arguments and resolve configuration
  2. Execute early-exit commands (--status, --verify, --restore, --show-setup)
  3. Run pre-backup hooks
  4. Execute selected backup modes (local, SSH, S3, database)
  5. Save manifests, encrypt, deduplicate, and apply retention policies
  6. Run post-backup hooks and send notifications
  7. Support scheduled mode with configurable times and graceful shutdown

All backup operations are orchestrated through ``backup_operation()`` which
handles both one-off CLI invocations and scheduled recurring runs.
"""

from src.cli import main

if __name__ == "__main__":
    main()
