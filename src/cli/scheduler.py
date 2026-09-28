"""
scheduler.py - Long-running scheduled mode.
"""

import signal
import sys
import time
from datetime import datetime

from ..config import extract_config_values
from .common import _acquire_lock
from .pipeline import _check_backup_dirs_accessible, backup_operation


def scheduled_operation(logger, config_file, telegram_bot=None, exclude_patterns=None, retain=None):
    """
    Run backups on a configurable schedule with graceful shutdown support.

    Acquires a PID lock to prevent duplicate instances, then enters a polling
    loop that checks the current time against configured schedule times every
    30 seconds (matching the ±30s tolerance window). Handles SIGINT/SIGTERM
    for clean shutdown.

    Parameters:
        logger: Logger instance.
        config_file (str): Path to the INI configuration file.
        telegram_bot (TelegramBot, optional): Telegram bot for notifications.
        exclude_patterns (list, optional): Glob patterns to exclude.
        retain (int, optional): CLI override for max_count retention policy.
    """
    _acquire_lock(logger)

    # Handle SIGINT/SIGTERM for clean shutdown
    _shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal _shutdown_requested
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name}, shutting down scheduler gracefully...")
        _shutdown_requested = True

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        # Loading the config file (with schedule validation)
        config_values = extract_config_values(logger, config_file, require_schedule=True)

        # Access the schedule times and interval
        times = config_values.get("schedule_times", [])
        config_values.get("interval_minutes", 60)
        # Ensure all times are in the correct format
        scheduled_times = []
        for t in times:
            try:
                scheduled_times.append(datetime.strptime(t, "%H:%M").time())
            except ValueError:
                logger.error(f"Time format error for value: {t}")
                continue

        # Use CLI exclude patterns if provided, otherwise use config
        if exclude_patterns is None:
            exclude_patterns = config_values.get("exclude_patterns", [])

        logger.info(f"Scheduled times: {scheduled_times}")
        while not _shutdown_requested:
            now = datetime.now()
            current_time = now.time()
            logger.info(f"Current time: {current_time}")

            # Check for matching scheduled time within a ±30 second tolerance
            matched = False
            for scheduled_time in scheduled_times:
                scheduled_dt = now.replace(
                    hour=scheduled_time.hour, minute=scheduled_time.minute, second=0, microsecond=0
                )
                diff = abs((now - scheduled_dt).total_seconds())
                if diff <= 30:
                    matched = True
                    break
            if matched:
                logger.info("Scheduled time matched. Performing backup operation...")
                # Pre-flight: verify backup directories are accessible
                sched_backup_dirs = config_values.get("backup_dirs", [])
                if sched_backup_dirs:
                    inaccessible = _check_backup_dirs_accessible(logger, sched_backup_dirs)
                    if inaccessible:
                        msg = (
                            f"Scheduled backup aborted: destination(s) inaccessible: "
                            f"{', '.join(inaccessible)}. Check that the disk is mounted."
                        )
                        logger.error(msg)
                        if telegram_bot:
                            try:
                                telegram_bot.send_notification(msg)
                            except Exception as e:
                                logger.error(f"Failed to send Telegram notification: {e}")
                        time.sleep(30)
                        continue
                # Build operation_modes from config flags
                operation_modes = []
                if config_values.get("local_mode"):
                    operation_modes.append("local")
                if config_values.get("ssh_mode"):
                    operation_modes.append("ssh")
                if config_values.get("s3_mode"):
                    operation_modes.append("s3")
                if config_values.get("db_mode"):
                    operation_modes.append("db")
                rc = backup_operation(
                    logger,
                    source_dir=config_values["source_dir"],
                    backup_dirs=config_values["backup_dirs"],
                    ssh_servers=config_values.get("ssh_servers"),
                    operation_modes=operation_modes,
                    backup_mode=config_values["mode"],
                    compress=config_values["compress_type"],
                    receiver=config_values["receiver_emails"],
                    notifications=bool(telegram_bot),
                    telegram_bot=telegram_bot,
                    ssh_username=config_values.get("ssh_username"),
                    ssh_password=config_values.get("ssh_password"),
                    exclude_patterns=exclude_patterns,
                    retain=retain,
                    config_path=None,
                    config_values=config_values,
                )
                if rc:
                    logger.error(f"Scheduled run returned exit code {rc}; scheduler continues.")
            else:
                logger.info("No scheduled time matched.")
            # Wait for 30 seconds before checking again (matches tolerance window)
            time.sleep(30)

        logger.info("Scheduler stopped cleanly.")

    except Exception as e:
        logger.error(f"Error in scheduled_operation: {e}")
        sys.exit(1)
