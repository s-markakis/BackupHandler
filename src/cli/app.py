"""
app.py - CLI entry point: argument parsing and command routing.
"""

import logging
import os
import sys
from pathlib import Path

from colorama import init

from ..argparse_setup import setup_argparse, validate_args
from ..banner import print_banner
from ..config import extract_config_values
from ..installer import run_installer
from ..logger import AppLogger, new_run_id
from ..restore import restore_backup
from ..snapshot import create_snapshot, diff_snapshots, generate_restore_script
from ..telegram_bot import TelegramBot
from ..verify import print_verify_report, verify_backup_integrity
from .common import CONFIG_PATH, LOG_PATH, PROJECT_ROOT
from .pipeline import backup_operation
from .scheduler import scheduled_operation
from .status import show_status

# Initialize colorama with autoreset to ensure color codes are reset after each print
init(autoreset=True)


# ─── Configuration Resolution ───────────────────────────────────────────────


def _resolve_config_path(args):
    """
    Resolve the configuration file path from CLI arguments.

    Priority: ``--profile`` > ``--config`` > default ``config/config.ini``.
    Profiles resolve to ``config/config.<name>.ini``.
    """
    if args.profile:
        profile_path = str(PROJECT_ROOT / "config" / f"config.{args.profile}.ini")
        if not os.path.exists(profile_path):
            print(f"Error: Profile config not found: {profile_path}", file=sys.stderr)
            sys.exit(1)
        return profile_path
    if args.config and os.path.exists(args.config):
        return args.config
    return CONFIG_PATH


# ─── Main Entry Point ───────────────────────────────────────────────────────


def main():
    """CLI entry point — parses arguments, routes to the appropriate operation."""
    # Initialize the logger BEFORE anything else (banner, argparse, filesystem).
    # On 2026-04-16 the script crashed in a pre-logger code path and 16 days of
    # cron firings produced zero log lines. Logger first, always.
    logger = AppLogger(LOG_PATH, logging.DEBUG).logger
    new_run_id()
    try:
        print_banner()
    except Exception as e:
        logger.warning(f"Banner failed (non-fatal): {e}")
    args = setup_argparse()

    # Validate the parsed arguments
    validate_args(args, logger)

    # Resolve config path (--config, --profile, or default)
    config_path = _resolve_config_path(args)

    # Handle --install early exit. Runs BEFORE AppLogger / banner write
    # anything to disk because the installer is invoked via sudo and we
    # do not want root-owned files left behind in the project tree.
    if args.install:
        try:
            install_config = extract_config_values(logger, config_path, skip_validation=True)
        except Exception as e:
            logger.error(f"Cannot load config for installer: {e}")
            sys.exit(1)
        rc = run_installer(install_config, PROJECT_ROOT, dry_run=args.dry_run)
        sys.exit(rc)

    # Handle --status early exit
    if args.status:
        show_status(logger, config_path)
        return

    # Handle --verify early exit
    if args.verify:
        try:
            verify_config = extract_config_values(logger, config_path, skip_validation=True)
        except Exception:
            verify_config = {}
        backup_dirs = args.backup_dirs or verify_config.get("backup_dirs", [])
        if not backup_dirs:
            logger.error(
                "No backup directories to verify. Specify --backup-dirs or configure [BACKUPS] backup_dirs."
            )
            sys.exit(1)
        enc_passphrase = verify_config.get("encryption_passphrase")
        enc_key_file = verify_config.get("encryption_key_file")
        results = verify_backup_integrity(
            logger, backup_dirs, encryption_passphrase=enc_passphrase, encryption_key_file=enc_key_file
        )
        all_ok = print_verify_report(results)
        sys.exit(0 if all_ok else 1)

    # Handle --snapshot early exit
    if args.snapshot:
        output_path = args.snapshot_output or str(PROJECT_ROOT / "snapshots")
        snapshot_file = create_snapshot(logger, output_dir=output_path)
        print(f"\nSnapshot saved to: {snapshot_file}")
        return

    # Handle --restore-snapshot early exit
    if args.restore_snapshot:
        output = args.snapshot_output
        if output is None:
            snapshot_name = Path(args.restore_snapshot).stem
            output = str(PROJECT_ROOT / "snapshots" / f"{snapshot_name}_restore.sh")
        script_path = generate_restore_script(logger, args.restore_snapshot, output_path=output)
        if script_path:
            print(f"\nRestore script generated: {script_path}")
            print("Review it, then run: chmod +x restore.sh && sudo ./restore.sh")
        else:
            print("Failed to generate restore script.", file=sys.stderr)
            sys.exit(1)
        return

    # Handle --snapshot-diff early exit
    if args.snapshot_diff:
        diff = diff_snapshots(logger, args.snapshot_diff[0], args.snapshot_diff[1])
        if not diff:
            print("\nNo differences found between snapshots.")
        else:
            print("\n=== Snapshot Diff ===\n")
            for category, changes in diff.items():
                added = changes.get("added", [])
                removed = changes.get("removed", [])
                print(f"  {category}:")
                for item in added:
                    print(f"    + {item}")
                for item in removed:
                    print(f"    - {item}")
                print()
        return

    # Handle --restore early exit
    if args.restore:
        # Load config to get encryption/SSH/S3 params for restore
        try:
            restore_config = extract_config_values(logger, config_path, skip_validation=True)
        except Exception:
            restore_config = {}
        enc_passphrase = restore_config.get("encryption_passphrase")
        enc_key_file = restore_config.get("encryption_key_file")

        logger.info(f"Restoring from {args.from_dir} to {args.to_dir}")
        success = restore_backup(
            logger,
            args.from_dir,
            args.to_dir,
            timestamp=args.restore_timestamp,
            encryption_passphrase=enc_passphrase,
            encryption_key_file=enc_key_file,
            ssh_password=restore_config.get("ssh_password"),
            s3_region=restore_config.get("s3_region"),
            s3_access_key=restore_config.get("s3_access_key"),
            s3_secret_key=restore_config.get("s3_secret_key"),
            dry_run=args.dry_run,
            ssh_key_file=restore_config.get("ssh_key_file"),
            strict_host_key_checking=restore_config.get("ssh_strict_host_key_checking", False),
            known_hosts_file=restore_config.get("ssh_known_hosts_file"),
        )
        if success:
            logger.info("Restore completed successfully.")
            print("Restore completed successfully.")
        else:
            logger.error("Restore completed with errors.")
            print("Restore completed with errors.", file=sys.stderr)
            sys.exit(1)
        return

    # Initialize TelegramBot if --notifications flag is used
    telegram_bot = None
    if args.notifications:
        try:
            telegram_bot = TelegramBot(logger)
        except FileNotFoundError:
            logger.error(
                "Telegram bot config not found. Create config/bot_config.ini from config/bot_config.ini.example"
            )
            print(
                "Error: config/bot_config.ini not found. Copy config/bot_config.ini.example and fill in your values.",
                file=sys.stderr,
            )
            sys.exit(1)
        except KeyError as e:
            logger.error(
                f"Missing key in bot_config.ini: {e}. Check that [TELEGRAM] api_token and [USERS] interacted_users are set."
            )
            print(
                f"Error: Missing key in config/bot_config.ini: {e}. Ensure api_token and interacted_users are set.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Use the provided receiver emails if notifications are enabled
    receiver_emails = args.receiver if args.notifications else None

    # Parse exclude patterns from CLI (overrides config)
    exclude_patterns = None
    if args.exclude:
        exclude_patterns = [p.strip() for p in args.exclude.split(",") if p.strip()]

    if args.scheduled:
        try:
            scheduled_operation(
                logger,
                config_path,
                telegram_bot=telegram_bot,
                exclude_patterns=exclude_patterns,
                retain=args.retain,
            )
        except Exception as e:
            logger.error(f"Failed to load configuration file: {config_path}. Error: {e}")
            sys.exit(1)
    else:
        # Fall back to config-defined source_dir / backup_dirs when CLI omits them.
        # Lets cron lines pass --config and skip --source-dir / --backup-dirs.
        cli_source_dir = args.source_dir
        cli_backup_dirs = args.backup_dirs
        if not cli_source_dir or not cli_backup_dirs:
            try:
                _cv = extract_config_values(logger, config_path, skip_validation=True)
            except Exception:
                _cv = {}
            cli_source_dir = cli_source_dir or _cv.get("source_dir")
            cli_backup_dirs = cli_backup_dirs or _cv.get("backup_dirs")
        if args.backup_mode and (not cli_source_dir or not cli_backup_dirs):
            logger.error(
                "Source directory and backup directories must be specified when using --backup-mode."
            )
            sys.exit(1)
        rc = backup_operation(
            logger,
            source_dir=cli_source_dir,
            backup_dirs=cli_backup_dirs,
            ssh_servers=args.ssh_servers,
            operation_modes=args.operation_modes,
            backup_mode=args.backup_mode,
            compress=args.compress,
            receiver=receiver_emails,
            show_setup=args.show_setup,
            notifications=args.notifications,
            telegram_bot=telegram_bot,
            dry_run=args.dry_run,
            exclude_patterns=exclude_patterns,
            retain=args.retain,
            config_path=config_path,
            encrypt=args.encrypt,
            dedup=args.dedup,
            tailscale=args.tailscale,
            tailscale_authkey=args.tailscale_authkey,
        )
        if rc:
            sys.exit(rc)
