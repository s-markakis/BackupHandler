"""
status.py - Backup status dashboard (--status).
"""

from datetime import datetime
from pathlib import Path

from ..config import extract_config_values
from ..manifest import load_latest_manifest
from ..utils import (
    get_last_backup_time,
    get_last_full_backup_time,
)


def show_status(logger, config_path):
    """
    Display a backup status dashboard including last backup timestamps,
    scheduled times, backup directory sizes, and latest manifest summary.
    """
    print("\n=== Backup Status ===\n")

    # Last backup timestamps
    last_backup = get_last_backup_time()
    last_full = get_last_full_backup_time()

    if last_backup:
        print(f"Last backup:      {datetime.fromtimestamp(last_backup).strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("Last backup:      Never")

    if last_full:
        print(f"Last full backup: {datetime.fromtimestamp(last_full).strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("Last full backup: Never")

    # Load config for schedule and backup dirs
    try:
        config_values = extract_config_values(logger, config_path, skip_validation=True)
    except Exception:
        config_values = {}

    # Scheduled times
    schedule_times = config_values.get("schedule_times", [])
    if schedule_times:
        print(f"\nScheduled times: {', '.join(schedule_times)}")
    else:
        print("\nScheduled times: Not configured")

    # Backup directory sizes
    backup_dirs = config_values.get("backup_dirs", [])
    if backup_dirs:
        print("\nBackup directories:")
        for bdir in backup_dirs:
            bpath = Path(bdir)
            if bpath.exists():
                total_size = sum(f.stat().st_size for f in bpath.rglob("*") if f.is_file())
                # Human-readable size
                if total_size >= 1073741824:
                    size_str = f"{total_size / 1073741824:.2f} GB"
                elif total_size >= 1048576:
                    size_str = f"{total_size / 1048576:.2f} MB"
                elif total_size >= 1024:
                    size_str = f"{total_size / 1024:.2f} KB"
                else:
                    size_str = f"{total_size} B"
                print(f"  {bdir}: {size_str}")
            else:
                print(f"  {bdir}: (not found)")

    # Latest manifest summary
    if backup_dirs:
        print("\nLatest manifest:")
        found_manifest = False
        for bdir in backup_dirs:
            manifest = load_latest_manifest(bdir)
            if manifest:
                found_manifest = True
                print(f"  Directory: {bdir}")
                print(f"    Timestamp: {manifest.get('timestamp', 'Unknown')}")
                print(f"    Mode:      {manifest.get('mode', 'Unknown')}")
                print(f"    Duration:  {manifest.get('duration_seconds', 0):.1f}s")
                print(f"    Copied:    {manifest.get('files_copied', 0)} files")
                print(f"    Skipped:   {manifest.get('files_skipped', 0)} files")
                print(f"    Failed:    {manifest.get('files_failed', 0)} files")
                total_bytes = manifest.get("total_bytes", 0)
                if total_bytes >= 1048576:
                    print(f"    Size:      {total_bytes / 1048576:.2f} MB")
                else:
                    print(f"    Size:      {total_bytes / 1024:.2f} KB")
                break
        if not found_manifest:
            print("  No manifests found")

    print()
