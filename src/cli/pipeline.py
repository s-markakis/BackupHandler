"""
pipeline.py - Core backup pipeline for a single run.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import extract_config_values
from ..db_sync import perform_db_backup
from ..dedup import deduplicate_backup_dirs
from ..encryption import EncryptionKey, encrypt_directories
from ..heartbeat import send_heartbeat
from ..logger import current_run_id
from ..manifest import BackupManifest
from ..preflight import (
    PreflightConfig,
    run_preflight,
    write_status_sentinel,
)
from ..retention import cleanup_old_backups
from ..s3_sync import sync_to_s3
from ..sync import (
    perform_differential_backup,
    perform_full_backup,
    perform_incremental_backup,
    sync_ssh_servers_concurrently,
)
from ..tailscale import tailscale_down, tailscale_up
from ..utils import (
    get_last_backup_time,
    get_last_full_backup_time,
    run_hook,
    update_last_backup_time,
    update_last_full_backup_time,
)
from .common import CONFIG_PATH, PROJECT_ROOT
from .notify import _critical_alert, _notify


def _run_backup(logger, telegram_bot, notifications, mode_name, backup_fn, config_values=None):
    """
    Execute a backup function with standardized notification and error handling.

    Wraps the actual backup call in a try/except to ensure failure notifications
    are always sent, even if the backup raises an unexpected exception. Returns
    True on success, False on failure so the caller can track partial failures
    and propagate a non-zero exit code.
    """
    try:
        backup_fn()
        _notify(
            logger,
            telegram_bot,
            notifications,
            f"Local {mode_name} backup completed.",
            config_values=config_values,
        )
        return True
    except Exception as e:
        logger.error(f"{mode_name.capitalize()} backup failed: {e}")
        _notify(
            logger,
            telegram_bot,
            notifications,
            f"{mode_name.capitalize()} backup failed.",
            config_values=config_values,
        )
        return False


# ─── Pre-flight Checks ─────────────────────────────────────────────────────


def _check_backup_dirs_accessible(logger, backup_dirs):
    """
    Verify that backup directories are accessible before starting a backup.

    For paths under a mount point (e.g. /mnt/*), checks that the mount point
    is actually mounted. Also ensures each backup directory exists or can be
    created. Returns a list of inaccessible directories (empty = all OK).
    """
    inaccessible = []
    for bdir in backup_dirs or []:
        bpath = Path(bdir)
        # Check if the path is under a mount point (e.g. /mnt/data/...)
        parts = bpath.parts
        if len(parts) >= 3 and parts[1] == "mnt":
            mount_point = Path("/") / parts[1] / parts[2]  # e.g. /mnt/data
            if not os.path.ismount(str(mount_point)):
                logger.error(
                    f"Mount point {mount_point} is not mounted. Backup directory {bdir} is inaccessible."
                )
                inaccessible.append(bdir)
                continue
        # Check if the directory exists or its parent is writable
        if not bpath.exists():
            try:
                bpath.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created backup directory: {bdir}")
            except OSError as e:
                logger.error(f"Cannot create backup directory {bdir}: {e}")
                inaccessible.append(bdir)
    return inaccessible


# ─── Core Backup Pipeline ───────────────────────────────────────────────────


@dataclass
class RunContext:
    """Everything one backup run's mode handlers need."""

    backup_dirs: Any
    backup_mode: Any
    bandwidth_limit: Any
    compress: Any
    config_values: Any
    dedup: Any
    dry_run: Any
    encrypt: Any
    exclude_patterns: Any
    logger: Any
    manifest: Any
    mode_failures: Any
    notifications: Any
    operation_modes: Any
    parallel_copies: Any
    receiver: Any
    s3_access_key: Any
    s3_bucket: Any
    s3_prefix: Any
    s3_region: Any
    s3_secret_key: Any
    source_dir: Any
    ssh_password: Any
    ssh_servers: Any
    ssh_username: Any
    tailscale: Any
    tailscale_authkey: Any
    telegram_bot: Any
    remote_key: Any = None


def _run_local_mode(ctx: RunContext):
    """Run the local full / incremental / differential backup."""
    backup_dirs = ctx.backup_dirs
    backup_mode = ctx.backup_mode
    compress = ctx.compress
    config_values = ctx.config_values
    dry_run = ctx.dry_run
    exclude_patterns = ctx.exclude_patterns
    logger = ctx.logger
    manifest = ctx.manifest
    mode_failures = ctx.mode_failures
    notifications = ctx.notifications
    operation_modes = ctx.operation_modes
    parallel_copies = ctx.parallel_copies
    receiver = ctx.receiver
    source_dir = ctx.source_dir
    telegram_bot = ctx.telegram_bot
    # Execute selected backup modes
    if "local" in operation_modes:
        if not backup_dirs:
            logger.warning("Local mode selected but no backup directories specified. Skipping local backup.")
        elif dry_run:
            logger.info(
                f"[DRY RUN] Would perform {backup_mode or 'full'} backup: '{source_dir}' -> {backup_dirs}"
            )
            print(f"[DRY RUN] Would perform {backup_mode or 'full'} backup")
            print(f"  Source:      {source_dir}")
            print(f"  Destinations: {', '.join(backup_dirs)}")
            if compress:
                print(f"  Compression: {compress}")
            if exclude_patterns:
                print(f"  Excluding:   {', '.join(exclude_patterns)}")
        elif backup_mode == "incremental":
            if compress:
                logger.error("Invalid option for incremental backup.")
                sys.exit(1)
            last_backup_time = get_last_backup_time()
            if not _run_backup(
                logger,
                telegram_bot,
                notifications,
                "incremental",
                lambda: perform_incremental_backup(
                    logger,
                    source_dir,
                    backup_dirs,
                    last_backup_time,
                    bot=telegram_bot,
                    receiver_emails=receiver,
                    exclude_patterns=exclude_patterns,
                    manifest=manifest,
                ),
                config_values=config_values,
            ):
                mode_failures.append("local-incremental")
        elif backup_mode == "differential":
            if compress:
                logger.error("Invalid option for differential backup.")
                sys.exit(1)
            last_full_backup_time = get_last_full_backup_time()
            if not _run_backup(
                logger,
                telegram_bot,
                notifications,
                "differential",
                lambda: perform_differential_backup(
                    logger,
                    source_dir,
                    backup_dirs,
                    last_full_backup_time,
                    bot=telegram_bot,
                    receiver_emails=receiver,
                    exclude_patterns=exclude_patterns,
                    manifest=manifest,
                ),
                config_values=config_values,
            ):
                mode_failures.append("local-differential")
        else:
            _notify(
                logger, telegram_bot, notifications, "Starting full backup...", config_values=config_values
            )
            if _run_backup(
                logger,
                telegram_bot,
                notifications,
                "full",
                lambda: perform_full_backup(
                    logger,
                    source_dir,
                    backup_dirs,
                    compress=compress,
                    bot=telegram_bot,
                    receiver_emails=receiver,
                    exclude_patterns=exclude_patterns,
                    manifest=manifest,
                    parallel_copies=parallel_copies,
                    send_zip_password=config_values.get("send_zip_password", True),
                ),
                config_values=config_values,
            ):
                update_last_full_backup_time()
            else:
                mode_failures.append("local-full")


def _prepare_remote_key(ctx: RunContext):
    """Derive the client-side key for SSH/S3 uploads (None when not encrypting)."""
    config_values = ctx.config_values
    dry_run = ctx.dry_run
    encrypt = ctx.encrypt
    logger = ctx.logger
    mode_failures = ctx.mode_failures
    operation_modes = ctx.operation_modes
    # Client-side encryption for remote targets: derive the key once and
    # upload ciphertext only, so plaintext never reaches SSH hosts or S3.
    remote_key = None
    wants_remote = operation_modes and ({"ssh", "s3"} & set(operation_modes)) and not dry_run
    if wants_remote and (encrypt or config_values.get("encryption_enabled", False)):
        if config_values.get("encryption_remote", True):
            try:
                remote_key = EncryptionKey.create(
                    passphrase=config_values.get("encryption_passphrase"),
                    key_file=config_values.get("encryption_key_file"),
                )
            except (ValueError, OSError) as e:
                logger.error(f"Cannot prepare encryption key for remote uploads: {e}")
                mode_failures.append("encryption")
        else:
            logger.warning("[ENCRYPTION] remote = False — SSH/S3 targets receive plaintext files.")
    return remote_key


def _run_ssh_mode(ctx: RunContext):
    """Run the SSH/SFTP backup, bringing Tailscale up and down if configured."""
    bandwidth_limit = ctx.bandwidth_limit
    config_values = ctx.config_values
    dry_run = ctx.dry_run
    exclude_patterns = ctx.exclude_patterns
    logger = ctx.logger
    manifest = ctx.manifest
    mode_failures = ctx.mode_failures
    notifications = ctx.notifications
    operation_modes = ctx.operation_modes
    remote_key = ctx.remote_key
    source_dir = ctx.source_dir
    # CLI arguments win; otherwise fall back to [SSH] in config.ini (the
    # non-scheduled CLI path previously ignored the config credentials).
    ssh_password = ctx.ssh_password or config_values.get("ssh_password")
    ssh_servers = ctx.ssh_servers or config_values.get("ssh_servers")
    ssh_username = ctx.ssh_username or config_values.get("ssh_username")
    tailscale = ctx.tailscale
    tailscale_authkey = ctx.tailscale_authkey
    telegram_bot = ctx.telegram_bot
    # Resolve Tailscale settings (CLI flags override config)
    ts_enabled = tailscale or config_values.get("tailscale_enabled", False)
    ts_auth_key = tailscale_authkey or config_values.get("tailscale_auth_key")
    ts_hostname = config_values.get("tailscale_hostname")
    ts_tags = config_values.get("tailscale_advertise_tags")
    ts_accept_routes = config_values.get("tailscale_accept_routes", False)
    ts_disconnect_after = config_values.get("tailscale_disconnect_after", False)
    _ts_brought_up = False

    if operation_modes and ("ssh" in operation_modes):
        if not ssh_servers:
            logger.warning("SSH mode selected but no SSH servers specified. Skipping SSH backup.")
        elif dry_run:
            logger.info(f"[DRY RUN] Would sync '{source_dir}' to SSH servers: {ssh_servers}")
            print(f"[DRY RUN] Would sync '{source_dir}' to SSH servers: {', '.join(ssh_servers)}")
            if ts_enabled:
                print(
                    f"[DRY RUN] Would connect via Tailscale VPN (auth_key: {'set' if ts_auth_key else 'not set'})"
                )
        else:
            # Bring up Tailscale before SSH if enabled
            if ts_enabled:
                if not ts_auth_key:
                    logger.error(
                        "Tailscale enabled but no auth key provided. "
                        "Set --tailscale-authkey or [TAILSCALE] auth_key in config."
                    )
                    _notify(
                        logger,
                        telegram_bot,
                        notifications,
                        "SSH backup aborted: Tailscale auth key missing.",
                        config_values=config_values,
                    )
                    mode_failures.append("ssh")
                else:
                    _ts_brought_up = tailscale_up(
                        ts_auth_key,
                        logger=logger,
                        hostname=ts_hostname,
                        advertise_tags=ts_tags,
                        accept_routes=ts_accept_routes,
                    )
                    if not _ts_brought_up:
                        logger.error("Failed to establish Tailscale connection. Aborting SSH backup.")
                        _notify(
                            logger,
                            telegram_bot,
                            notifications,
                            "SSH backup aborted: Tailscale connection failed.",
                            config_values=config_values,
                        )
                        mode_failures.append("ssh")

            # Only proceed with SSH if Tailscale is not required or connected successfully
            if not ts_enabled or _ts_brought_up:
                _notify(
                    logger,
                    telegram_bot,
                    notifications,
                    f"Starting SSH backup{' via Tailscale' if ts_enabled else ''}...",
                    config_values=config_values,
                )
                logger.info("Running SSH backup...")
                try:
                    failed_servers = sync_ssh_servers_concurrently(
                        source_dir,
                        ssh_servers,
                        username=ssh_username or "",
                        password=ssh_password,
                        key_filepath=config_values.get("ssh_key_file"),
                        logger=logger,
                        exclude_patterns=exclude_patterns,
                        manifest=manifest,
                        bandwidth_limit=bandwidth_limit,
                        encryption_key=remote_key,
                        strict_host_key_checking=config_values.get("ssh_strict_host_key_checking", False),
                        known_hosts_file=config_values.get("ssh_known_hosts_file"),
                    )
                    if failed_servers:
                        raise RuntimeError(f"SSH sync failed for: {', '.join(failed_servers)}")
                    _notify(
                        logger,
                        telegram_bot,
                        notifications,
                        "SSH backup completed.",
                        config_values=config_values,
                    )
                except Exception as e:
                    logger.error(f"SSH backup failed: {e}")
                    _notify(
                        logger, telegram_bot, notifications, "SSH backup failed.", config_values=config_values
                    )
                    mode_failures.append("ssh")
                finally:
                    # Disconnect Tailscale after SSH backup if configured
                    if ts_enabled and _ts_brought_up and ts_disconnect_after:
                        tailscale_down(logger=logger)


def _run_s3_mode(ctx: RunContext):
    """Run the S3 backup."""
    backup_mode = ctx.backup_mode
    config_values = ctx.config_values
    dry_run = ctx.dry_run
    exclude_patterns = ctx.exclude_patterns
    logger = ctx.logger
    manifest = ctx.manifest
    mode_failures = ctx.mode_failures
    notifications = ctx.notifications
    operation_modes = ctx.operation_modes
    remote_key = ctx.remote_key
    s3_access_key = ctx.s3_access_key
    s3_bucket = ctx.s3_bucket
    s3_prefix = ctx.s3_prefix
    s3_region = ctx.s3_region
    s3_secret_key = ctx.s3_secret_key
    source_dir = ctx.source_dir
    telegram_bot = ctx.telegram_bot
    if operation_modes and ("s3" in operation_modes):
        if not s3_bucket:
            logger.warning("S3 mode selected but no bucket configured. Skipping S3 backup.")
        elif dry_run:
            logger.info(f"[DRY RUN] Would sync '{source_dir}' to s3://{s3_bucket}/{s3_prefix}")
            print(f"[DRY RUN] Would sync '{source_dir}' to s3://{s3_bucket}/{s3_prefix}")
        else:
            _notify(logger, telegram_bot, notifications, "Starting S3 backup...", config_values=config_values)
            logger.info("Running S3 backup...")
            try:
                s3_ok = sync_to_s3(
                    logger,
                    source_dir,
                    s3_bucket,
                    prefix=s3_prefix,
                    region=s3_region,
                    access_key=s3_access_key,
                    secret_key=s3_secret_key,
                    mode=backup_mode or "full",
                    exclude_patterns=exclude_patterns,
                    manifest=manifest,
                    max_bandwidth=config_values.get("s3_max_bandwidth"),
                    multipart_threshold=config_values.get("s3_multipart_threshold"),
                    max_concurrency=config_values.get("s3_max_concurrency"),
                    encryption_key=remote_key,
                    server_side_encryption=config_values.get("s3_server_side_encryption"),
                    sse_kms_key_id=config_values.get("s3_sse_kms_key_id"),
                )
                if not s3_ok:
                    raise RuntimeError("one or more files failed to upload")
                _notify(
                    logger, telegram_bot, notifications, "S3 backup completed.", config_values=config_values
                )
            except Exception as e:
                logger.error(f"S3 backup failed: {e}")
                _notify(logger, telegram_bot, notifications, "S3 backup failed.", config_values=config_values)
                mode_failures.append("s3")


def _run_db_mode(ctx: RunContext):
    """Run the MySQL dump."""
    backup_dirs = ctx.backup_dirs
    config_values = ctx.config_values
    dry_run = ctx.dry_run
    logger = ctx.logger
    manifest = ctx.manifest
    mode_failures = ctx.mode_failures
    notifications = ctx.notifications
    operation_modes = ctx.operation_modes
    telegram_bot = ctx.telegram_bot
    if operation_modes and ("db" in operation_modes):
        db_database = config_values.get("db_database")
        if not db_database:
            logger.warning(
                "DB mode selected but no database configured in [DATABASE]. Skipping database backup."
            )
        elif dry_run:
            perform_db_backup(logger, config_values, backup_dirs or [], manifest, dry_run=True)
        else:
            _notify(
                logger,
                telegram_bot,
                notifications,
                "Starting database backup...",
                config_values=config_values,
            )
            logger.info("Running database backup...")
            try:
                success = perform_db_backup(logger, config_values, backup_dirs or [], manifest)
                if success:
                    _notify(
                        logger,
                        telegram_bot,
                        notifications,
                        "Database backup completed.",
                        config_values=config_values,
                    )
                else:
                    _notify(
                        logger,
                        telegram_bot,
                        notifications,
                        "Database backup failed.",
                        config_values=config_values,
                    )
                    mode_failures.append("db")
            except Exception as e:
                logger.error(f"Database backup failed: {e}")
                _notify(
                    logger,
                    telegram_bot,
                    notifications,
                    "Database backup failed.",
                    config_values=config_values,
                )
                mode_failures.append("db")


def _finalize_backup_dirs(ctx: RunContext):
    """Save manifests, then deduplicate and encrypt the local backup directories."""
    backup_dirs = ctx.backup_dirs
    compress = ctx.compress
    config_values = ctx.config_values
    dedup = ctx.dedup
    encrypt = ctx.encrypt
    logger = ctx.logger
    manifest = ctx.manifest
    mode_failures = ctx.mode_failures
    # Save manifest to each backup directory
    if backup_dirs:
        for bdir in backup_dirs:
            try:
                manifest_path = manifest.save(bdir)
                logger.info(f"Backup manifest saved to {manifest_path}")
            except Exception as e:
                logger.error(f"Failed to save manifest to {bdir}: {e}")

    # Warn about compression + encryption interaction
    if compress and compress != "none":
        enc_check = encrypt or config_values.get("encryption_enabled", False)
        if enc_check:
            logger.warning(
                "Both compression and encryption are enabled. "
                "Encrypted data does not compress well — compression runs first, "
                "then encryption is applied to the compressed archive."
            )

    # Deduplicate before encrypting: ciphertext is unique per file, so dedup
    # after encryption can never find anything. Encryption preserves the
    # hardlinks dedup creates.
    dedup_enabled = dedup or config_values.get("dedup_enabled", False)
    if dedup_enabled and backup_dirs:
        try:
            dedup_result = deduplicate_backup_dirs(logger, backup_dirs)
            if dedup_result["duplicates_found"] > 0:
                logger.info(
                    f"Dedup: {dedup_result['duplicates_found']} duplicates, "
                    f"{dedup_result['bytes_saved']} bytes saved"
                )
        except Exception as e:
            logger.error(f"Deduplication failed: {e}")

    # Encrypt backup files (after manifest save and dedup, before retention)
    encryption_enabled = encrypt or config_values.get("encryption_enabled", False)
    enc_passphrase = config_values.get("encryption_passphrase")
    enc_key_file = config_values.get("encryption_key_file")
    enc_workers = config_values.get("encryption_workers", 1)

    if encryption_enabled and backup_dirs:
        if not enc_passphrase and not enc_key_file:
            logger.error("Encryption enabled but no passphrase or key_file configured in [ENCRYPTION].")
            mode_failures.append("encryption")
        else:
            try:
                enc_result = encrypt_directories(
                    backup_dirs,
                    passphrase=enc_passphrase,
                    key_file=enc_key_file,
                    logger=logger,
                    workers=enc_workers,
                )
                logger.info(f"Encrypted {enc_result.succeeded} files in {', '.join(backup_dirs)}")
                if enc_result.failed:
                    # Plaintext copies remain on disk for these files — a real failure.
                    logger.error(
                        f"Encryption failed for {enc_result.failed} file(s); plaintext left in backup"
                    )
                    mode_failures.append("encryption")
            except Exception as e:
                logger.error(f"Encryption failed: {e}")
                mode_failures.append("encryption")


def backup_operation(
    logger,
    source_dir=None,
    backup_dirs=None,
    ssh_servers=None,
    operation_modes=None,
    backup_mode=None,
    compress=None,
    receiver=None,
    show_setup=False,
    notifications=False,
    telegram_bot=None,
    ssh_username=None,
    ssh_password=None,
    dry_run=False,
    exclude_patterns=None,
    retain=None,
    config_path=None,
    config_values=None,
    encrypt=False,
    dedup=False,
    tailscale=False,
    tailscale_authkey=None,
):
    """
    Orchestrate the full backup pipeline for a single run.

    Execution order:
      1. Pre-backup hook (failure aborts the run)
      2. Local / SSH / S3 / Database backup modes (based on ``operation_modes``)
      3. Save backup manifests to each backup directory
      4. Deduplicate via hardlinks (if enabled)
      5. Encrypt backup files (AES-256-GCM, if enabled; hardlinks preserved)
      6. Update backup timestamps
      7. Apply retention policies (age-based and count-based)
      8. Post-backup hook (failure is logged but does not affect backup status)
      9. Send completion notification

    All parameters can be sourced from CLI args, config file, or both (CLI wins).

    Returns a process exit code:
      0 - all selected modes succeeded (or dry-run / show-setup completed)
      2 - pre-flight failure (backup dir inaccessible, pre-hook failed)
      3 - at least one backup mode failed
    Callers (main / scheduled_operation) decide whether to ``sys.exit()`` or
    continue looping.
    """
    # Show setup command (skip validation so incomplete configs can be inspected)
    if show_setup:
        extract_config_values(logger, config_path or CONFIG_PATH, show=True, skip_validation=True)
        return 0

    # Load config values if not provided (for hooks, retention, parallel, bandwidth, S3)
    if config_values is None and config_path:
        try:
            config_values = extract_config_values(logger, config_path, skip_validation=True)
        except Exception:
            config_values = {}

    if config_values is None:
        config_values = {}

    # Use config exclude patterns if CLI didn't provide them
    if exclude_patterns is None:
        exclude_patterns = config_values.get("exclude_patterns", [])

    # ─── Preflight self-check + sentinel ───────────────────────────────────
    # Resolve sentinel path (relative paths anchor at project root).
    pf_cfg = PreflightConfig(
        enabled=config_values.get("preflight_enabled", True),
        expected_mount=config_values.get("preflight_expected_mount"),
        expected_label=config_values.get("preflight_expected_label"),
        expected_uuid=config_values.get("preflight_expected_uuid"),
        expected_fs_type=config_values.get("preflight_expected_fs_type", "xfs"),
        expected_owner=config_values.get("preflight_expected_owner"),
        auto_mount=config_values.get("preflight_auto_mount", True),
        auto_fix_ownership=config_values.get("preflight_auto_fix_ownership", False),
        ensure_fstab=config_values.get("preflight_ensure_fstab", True),
        staleness_factor=config_values.get("preflight_staleness_factor", 2.0),
        local_mail_to=config_values.get("preflight_local_mail_to"),
        status_sentinel=config_values.get("preflight_status_sentinel", "Logs/last_run_status.json"),
    )
    sentinel_relpath = Path(pf_cfg.status_sentinel)
    sentinel_path = sentinel_relpath if sentinel_relpath.is_absolute() else PROJECT_ROOT / sentinel_relpath

    write_status_sentinel(
        sentinel_path,
        status="started",
        run_id=current_run_id(),
        message="backup run started",
        extra={"backup_dirs": backup_dirs or [], "modes": operation_modes or []},
    )

    if not dry_run and not show_setup:
        pf_result = run_preflight(
            logger,
            pf_cfg,
            backup_dirs=backup_dirs or [],
            interval_minutes=config_values.get("interval_minutes", 60),
        )
        if not pf_result.ok:
            logger.error(f"Preflight FAILED: {pf_result.message}")
            _critical_alert(
                logger,
                config_values,
                telegram_bot,
                notifications,
                "Preflight FAILED",
                pf_result.message,
            )
            write_status_sentinel(
                sentinel_path,
                status="failure",
                run_id=current_run_id(),
                message=f"preflight: {pf_result.message}",
                extra={"phase": "preflight", "details": pf_result.details},
            )
            return 2
        if pf_result.healed:
            logger.info(f"Preflight self-healed: {pf_result.message}")
        stale_msg = pf_result.details.get("stale_alert") if pf_result.details else None
        if stale_msg:
            _critical_alert(
                logger,
                config_values,
                telegram_bot,
                notifications,
                "Backup STALE",
                stale_msg,
            )

    # Hooks
    pre_hook = config_values.get("pre_backup_hook")
    post_hook = config_values.get("post_backup_hook")

    # Retention (CLI --retain overrides config max_count)
    max_age_days = config_values.get("max_age_days", 0)
    max_count = retain if retain is not None else config_values.get("max_count", 0)

    # Parallel copies
    parallel_copies = config_values.get("parallel_copies", 1)

    # Bandwidth limit
    bandwidth_limit = config_values.get("bandwidth_limit", 0)

    # S3 config
    s3_bucket = config_values.get("s3_bucket")
    s3_prefix = config_values.get("s3_prefix", "")
    s3_region = config_values.get("s3_region")
    s3_access_key = config_values.get("s3_access_key")
    s3_secret_key = config_values.get("s3_secret_key")

    # Run pre-backup hook
    if pre_hook and not run_hook(logger, pre_hook, "pre_backup"):
        logger.error("Pre-backup hook failed. Aborting backup.")
        _critical_alert(
            logger,
            config_values,
            telegram_bot,
            notifications,
            "Backup aborted",
            "pre-backup hook failed.",
        )
        write_status_sentinel(
            sentinel_path,
            status="failure",
            run_id=current_run_id(),
            message="pre-backup hook failed",
            extra={"phase": "pre_hook"},
        )
        return 2

    if operation_modes is None:
        operation_modes = []

    # Track per-mode failures so we can exit non-zero if any mode failed.
    # Systemd and Prometheus rely on this to page an operator.
    mode_failures: list[str] = []

    # Create manifest for this backup run
    manifest = BackupManifest(mode=backup_mode or "full")

    ctx = RunContext(
        backup_dirs=backup_dirs,
        backup_mode=backup_mode,
        bandwidth_limit=bandwidth_limit,
        compress=compress,
        config_values=config_values,
        dedup=dedup,
        dry_run=dry_run,
        encrypt=encrypt,
        exclude_patterns=exclude_patterns,
        logger=logger,
        manifest=manifest,
        mode_failures=mode_failures,
        notifications=notifications,
        operation_modes=operation_modes,
        parallel_copies=parallel_copies,
        receiver=receiver,
        s3_access_key=s3_access_key,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        s3_region=s3_region,
        s3_secret_key=s3_secret_key,
        source_dir=source_dir,
        ssh_password=ssh_password,
        ssh_servers=ssh_servers,
        ssh_username=ssh_username,
        tailscale=tailscale,
        tailscale_authkey=tailscale_authkey,
        telegram_bot=telegram_bot,
    )

    _run_local_mode(ctx)
    ctx.remote_key = _prepare_remote_key(ctx)
    _run_ssh_mode(ctx)
    _run_s3_mode(ctx)
    _run_db_mode(ctx)

    if dry_run:
        # Show encryption info in dry-run
        dry_encrypt = encrypt or config_values.get("encryption_enabled", False)
        if dry_encrypt:
            enc_method = "key_file" if config_values.get("encryption_key_file") else "passphrase"
            print(f"[DRY RUN] Would encrypt backup files using AES-256-GCM ({enc_method})")
        dry_dedup = dedup or config_values.get("dedup_enabled", False)
        if dry_dedup:
            print("[DRY RUN] Would deduplicate identical files using hardlinks")
        logger.info("[DRY RUN] Complete. No files were modified.")
        print("\n[DRY RUN] Complete. No files were modified.")
        return 0

    _finalize_backup_dirs(ctx)

    # Update the backup timestamp only if every mode succeeded. A partial
    # success must not advance the "last good backup" marker — doing so
    # would let a broken mode silently skew future incremental windows.
    if not mode_failures:
        update_last_backup_time()

    # Run retention cleanup
    if backup_dirs and (max_age_days > 0 or max_count > 0):
        cleanup_old_backups(logger, backup_dirs, max_age_days=max_age_days, max_count=max_count)

    # Run post-backup hook
    if post_hook and not run_hook(logger, post_hook, "post_backup"):
        logger.warning("Post-backup hook failed (backup itself succeeded).")

    if mode_failures:
        failed_str = ", ".join(mode_failures)
        logger.error(f"Backup run finished with failures in: {failed_str}")
        _critical_alert(
            logger,
            config_values,
            telegram_bot,
            notifications,
            "Backup partial failure",
            f"failures in: {failed_str}",
        )
        write_status_sentinel(
            sentinel_path,
            status="failure",
            run_id=current_run_id(),
            message=f"failures in: {failed_str}",
            extra={"phase": "modes", "failed_modes": mode_failures},
        )
        return 3

    _notify(
        logger,
        telegram_bot,
        notifications,
        "All backup operations completed successfully.",
        config_values=config_values,
    )

    # Dead-man's-switch ping. Only on full success — a failed run must not
    # reset the heartbeat window, or the external watchdog will never page.
    hb_url = config_values.get("heartbeat_url")
    if hb_url:
        try:
            send_heartbeat(logger, hb_url, timeout=config_values.get("heartbeat_timeout", 10))
        except Exception as e:
            logger.error(f"Heartbeat dispatch failed (non-fatal): {e}")

    write_status_sentinel(
        sentinel_path,
        status="success",
        run_id=current_run_id(),
        message="all modes completed",
        extra={"modes": operation_modes or []},
    )
    return 0
