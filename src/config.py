"""
config.py - Configuration Loading, Validation, and Display

Loads INI-format configuration files via ``configparser``, resolves
``${ENV_VAR}`` placeholders from the environment, validates required
fields with clear error messages, and extracts all settings into a
normalized dictionary for use by the backup pipeline.

Supports conditional validation — SSH, S3, database, and encryption
fields are only validated when their respective mode is enabled.
"""

import configparser
import os
import re
import sys
from pathlib import Path

from src.utils import is_valid_email

# ─── Schema Version ─────────────────────────────────────────────────────────
CURRENT_SCHEMA_VERSION = "3"


# ─── Environment Variable Resolution ────────────────────────────────────────


def resolve_env_vars(value):
    """
    Replace ``${ENV_VAR}`` placeholders in a string with environment variable values.

    Parameters:
        value (str): Config value potentially containing ``${VAR}`` references.

    Returns:
        str: Value with all placeholders resolved.

    Raises:
        ValueError: If a referenced environment variable is not set.
    """

    def _replace(match):
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(
                f"Environment variable '{var_name}' is not set (referenced as '${{{var_name}}}' in config)"
            )
        return env_value

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _check_schema_version(config, logger):
    """
    Check the config file's schema version against the expected version.

    Logs a warning if the config is outdated or missing a [META] section,
    helping users identify when their config needs updating after an upgrade.
    """
    file_version = normalize_none(config.get("META", "schema_version", fallback=None))
    if file_version is None:
        logger.warning(
            f"Config file has no [META] schema_version. "
            f"Expected version {CURRENT_SCHEMA_VERSION}. "
            f"Add [META] schema_version = {CURRENT_SCHEMA_VERSION} to suppress this warning."
        )
    elif file_version != CURRENT_SCHEMA_VERSION:
        logger.warning(
            f"Config schema version mismatch: file has v{file_version}, "
            f"expected v{CURRENT_SCHEMA_VERSION}. Review config/config.ini.example for new options."
        )


def _resolve_all_env_vars(config, logger):
    """
    Walk all config sections (including DEFAULT) and resolve ``${ENV_VAR}``
    placeholders in-place. Exits with a clear error if any referenced
    variable is not set in the environment.
    """
    sections = ["DEFAULT", *config.sections()]
    for section in sections:
        for key in config[section]:
            raw_value = config.get(section, key, raw=True)
            if "${" in raw_value:
                try:
                    resolved = resolve_env_vars(raw_value)
                    config.set(section, key, resolved)
                    logger.debug(f"Resolved env var in [{section}].{key}")
                except ValueError as e:
                    logger.error(str(e))
                    print(f"\n  Config error: {e}", file=sys.stderr)
                    sys.exit(1)


# ─── Value Normalization ────────────────────────────────────────────────────


def normalize_none(value):
    """
    Normalize config values by converting sentinel strings to Python None.

    INI files represent unset values as the literal string ``None`` or empty
    strings. This function converts those to actual ``None`` for cleaner
    downstream handling.

    Returns:
        str or None: Stripped value, or None if empty/``None``.
    """
    if value is None:
        return None
    stripped = str(value).strip()
    if stripped.lower() == "none" or stripped == "":
        return None
    return stripped


# ─── Configuration Extraction ───────────────────────────────────────────────


def extract_config_values(
    logger, config_file_path, show=False, require_schedule=False, skip_validation=False
):
    """
    Load, validate, and extract all configuration values from an INI file.

    This is the primary entry point for configuration loading. It:
      1. Reads the INI file via ``configparser``
      2. Resolves ``${ENV_VAR}`` placeholders
      3. Validates required fields (unless ``skip_validation=True``)
      4. Normalizes values and resolves relative paths to absolute
      5. Returns a flat dictionary or prints a human-readable summary

    Parameters:
        logger: Logger instance.
        config_file_path (str): Path to the INI configuration file.
        show (bool): If True, print configuration to stdout instead of returning.
        require_schedule (bool): If True, validate schedule times are present.
        skip_validation (bool): If True, skip validation (for ``--show-setup``).

    Returns:
        dict or None: Configuration dictionary if ``show=False``, else None.
    """
    config = load_config(logger, config_file_path)

    # Check schema version for config compatibility warnings
    _check_schema_version(config, logger)

    # Resolve ${ENV_VAR} placeholders before validation
    _resolve_all_env_vars(config, logger)

    # Run validation before extracting values (skip for --show-setup)
    if not skip_validation:
        validate_config(logger, config, require_schedule=require_schedule)

    try:
        # Extract and clean values with defaults
        schedule_times = config.get("SCHEDULE", "times", fallback=None)

        # Normalize optional fields to avoid "None" strings
        raw_ssh_servers = normalize_none(config.get("SSH", "ssh_servers", fallback=None))
        raw_username = normalize_none(config.get("SSH", "username", fallback=None))
        raw_password = normalize_none(config.get("SSH", "password", fallback=None))
        ssh_key_file = normalize_none(config.get("SSH", "key_file", fallback=None))
        raw_receiver_emails = normalize_none(config.get("NOTIFICATIONS", "receiver_emails", fallback=None))

        # Exclude patterns from config
        raw_exclude = normalize_none(config.get("DEFAULT", "exclude_patterns", fallback=None))

        # Hooks
        pre_backup_hook = normalize_none(config.get("HOOKS", "pre_backup", fallback=None))
        post_backup_hook = normalize_none(config.get("HOOKS", "post_backup", fallback=None))

        # Retention
        max_age_days = config.getint("RETENTION", "max_age_days", fallback=0)
        max_count = config.getint("RETENTION", "max_count", fallback=0)

        # Parallel copies
        parallel_copies = config.getint("DEFAULT", "parallel_copies", fallback=1)

        # SSH bandwidth limit
        bandwidth_limit = config.getint("SSH", "bandwidth_limit", fallback=0)
        ssh_strict_host_key_checking = config.getboolean("SSH", "strict_host_key_checking", fallback=False)
        ssh_known_hosts_file = normalize_none(config.get("SSH", "known_hosts_file", fallback=None))

        # S3 config
        s3_bucket = normalize_none(config.get("S3", "bucket", fallback=None))
        s3_prefix = normalize_none(config.get("S3", "prefix", fallback=None)) or ""
        s3_region = normalize_none(config.get("S3", "region", fallback=None))
        s3_access_key = normalize_none(config.get("S3", "access_key", fallback=None))
        s3_secret_key = normalize_none(config.get("S3", "secret_key", fallback=None))
        s3_max_bandwidth = config.getint("S3", "max_bandwidth", fallback=0)
        s3_multipart_threshold = config.getint("S3", "multipart_threshold", fallback=8)
        s3_max_concurrency = config.getint("S3", "max_concurrency", fallback=10)
        s3_server_side_encryption = normalize_none(config.get("S3", "server_side_encryption", fallback=None))
        s3_sse_kms_key_id = normalize_none(config.get("S3", "sse_kms_key_id", fallback=None))

        # Encryption config
        encryption_enabled = config.getboolean("ENCRYPTION", "enabled", fallback=False)
        encryption_key_file = normalize_none(config.get("ENCRYPTION", "key_file", fallback=None))
        encryption_passphrase = normalize_none(config.get("ENCRYPTION", "passphrase", fallback=None))
        encryption_workers = config.getint("ENCRYPTION", "workers", fallback=1)
        encryption_remote = config.getboolean("ENCRYPTION", "remote", fallback=True)

        # Database config
        db_user = normalize_none(config.get("DATABASE", "user", fallback=None))
        db_password = normalize_none(config.get("DATABASE", "password", fallback=None))
        db_database = normalize_none(config.get("DATABASE", "database", fallback=None))
        db_host = normalize_none(config.get("DATABASE", "host", fallback=None)) or "localhost"
        db_port = config.getint("DATABASE", "port", fallback=3306)
        db_single_transaction = config.getboolean("DATABASE", "single_transaction", fallback=True)
        db_binlog_position = config.getboolean("DATABASE", "binlog_position", fallback=False)

        # SMTP config
        smtp_host = normalize_none(config.get("SMTP", "host", fallback=None))
        smtp_port = config.getint("SMTP", "port", fallback=587)
        smtp_user = normalize_none(config.get("SMTP", "user", fallback=None))
        smtp_password = normalize_none(config.get("SMTP", "password", fallback=None))
        smtp_from = normalize_none(config.get("SMTP", "from_addr", fallback=None))
        smtp_to = normalize_none(config.get("SMTP", "to_addrs", fallback=None))
        smtp_tls = config.getboolean("SMTP", "use_tls", fallback=True)

        # Whether zip_pw archive passwords are delivered via bot/email (always kept in keyring)
        send_zip_password = config.getboolean("NOTIFICATIONS", "send_zip_password", fallback=True)

        # Dedup config
        dedup_enabled = config.getboolean("DEDUP", "enabled", fallback=False)

        # Webhook config
        webhook_url = normalize_none(config.get("WEBHOOK", "url", fallback=None))
        webhook_auth_header = normalize_none(config.get("WEBHOOK", "auth_header", fallback=None))

        # Heartbeat / dead-man's-switch config (healthchecks.io, Dead Man's Snitch, etc).
        # On a successful run, we ping the URL. If the ping is missed, the external
        # service pages an operator — this catches silent failures like the host
        # being off or the unit being disabled.
        heartbeat_url = normalize_none(config.get("HEARTBEAT", "url", fallback=None))
        heartbeat_timeout = config.getint("HEARTBEAT", "timeout", fallback=10)

        # Preflight self-check / self-heal — destination mount verification,
        # ownership repair, fstab maintenance, status sentinel, local mail.
        # See src/preflight.py and RUNBOOK §5 for the full contract.
        preflight_enabled = config.getboolean("PREFLIGHT", "enabled", fallback=True)
        preflight_expected_mount = normalize_none(config.get("PREFLIGHT", "expected_mount", fallback=None))
        preflight_expected_label = normalize_none(config.get("PREFLIGHT", "expected_label", fallback=None))
        preflight_expected_uuid = normalize_none(config.get("PREFLIGHT", "expected_uuid", fallback=None))
        preflight_expected_fs_type = (
            normalize_none(config.get("PREFLIGHT", "expected_fs_type", fallback="xfs")) or "xfs"
        )
        preflight_expected_owner = normalize_none(config.get("PREFLIGHT", "expected_owner", fallback=None))
        preflight_auto_mount = config.getboolean("PREFLIGHT", "auto_mount", fallback=True)
        preflight_auto_fix_ownership = config.getboolean("PREFLIGHT", "auto_fix_ownership", fallback=False)
        preflight_ensure_fstab = config.getboolean("PREFLIGHT", "ensure_fstab", fallback=True)
        preflight_staleness_factor = config.getfloat("PREFLIGHT", "staleness_factor", fallback=2.0)
        preflight_local_mail_to = normalize_none(config.get("PREFLIGHT", "local_mail_to", fallback=None))
        preflight_status_sentinel = (
            normalize_none(config.get("PREFLIGHT", "status_sentinel", fallback="Logs/last_run_status.json"))
            or "Logs/last_run_status.json"
        )

        # Tailscale config
        tailscale_enabled = config.getboolean("TAILSCALE", "enabled", fallback=False)
        tailscale_auth_key = normalize_none(config.get("TAILSCALE", "auth_key", fallback=None))
        tailscale_hostname = normalize_none(config.get("TAILSCALE", "hostname", fallback=None))
        tailscale_advertise_tags = normalize_none(config.get("TAILSCALE", "advertise_tags", fallback=None))
        tailscale_accept_routes = config.getboolean("TAILSCALE", "accept_routes", fallback=False)
        tailscale_disconnect_after = config.getboolean("TAILSCALE", "disconnect_after", fallback=False)

        config_vars = {
            "source_dir": config.get("DEFAULT", "source_dir", fallback=None),
            "mode": config.get("DEFAULT", "mode", fallback="full"),
            "compress_type": config.get("DEFAULT", "compress_type", fallback="none"),
            "backup_dirs": [
                dir.strip()
                for dir in config.get("BACKUPS", "backup_dirs", fallback="").split(",")
                if dir.strip()
            ],
            "ssh_servers": (
                [s.strip() for s in raw_ssh_servers.split(",") if s.strip()] if raw_ssh_servers else []
            ),
            "ssh_username": raw_username,
            "ssh_password": raw_password,
            "ssh_key_file": ssh_key_file,
            "schedule_times": (
                [time.strip() for time in schedule_times.split(",") if time.strip()] if schedule_times else []
            ),
            "interval_minutes": config.getint("SCHEDULE", "interval_minutes", fallback=1),
            "local_mode": config.getboolean("MODES", "local", fallback=False),
            "ssh_mode": config.getboolean("MODES", "ssh", fallback=False),
            "s3_mode": config.getboolean("MODES", "s3", fallback=False),
            "bot": config.getboolean("NOTIFICATIONS", "bot", fallback=False),
            "receiver_emails": None,
            "exclude_patterns": (
                [p.strip() for p in raw_exclude.split(",") if p.strip()] if raw_exclude else []
            ),
            "pre_backup_hook": pre_backup_hook,
            "post_backup_hook": post_backup_hook,
            "max_age_days": max_age_days,
            "max_count": max_count,
            "parallel_copies": max(1, parallel_copies),
            "bandwidth_limit": max(0, bandwidth_limit),
            "ssh_strict_host_key_checking": ssh_strict_host_key_checking,
            "ssh_known_hosts_file": ssh_known_hosts_file,
            "s3_bucket": s3_bucket,
            "s3_prefix": s3_prefix,
            "s3_region": s3_region,
            "s3_access_key": s3_access_key,
            "s3_secret_key": s3_secret_key,
            "s3_max_bandwidth": s3_max_bandwidth if s3_max_bandwidth > 0 else None,
            "s3_multipart_threshold": s3_multipart_threshold,
            "s3_max_concurrency": s3_max_concurrency,
            "s3_server_side_encryption": s3_server_side_encryption,
            "s3_sse_kms_key_id": s3_sse_kms_key_id,
            "encryption_enabled": encryption_enabled,
            "encryption_key_file": encryption_key_file,
            "encryption_passphrase": encryption_passphrase,
            "encryption_workers": max(1, encryption_workers),
            "encryption_remote": encryption_remote,
            "db_mode": config.getboolean("MODES", "db", fallback=False),
            "db_user": db_user,
            "db_password": db_password,
            "db_database": db_database,
            "db_host": db_host,
            "db_port": db_port,
            "db_single_transaction": db_single_transaction,
            "db_binlog_position": db_binlog_position,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_user": smtp_user,
            "smtp_password": smtp_password,
            "smtp_from": smtp_from,
            "smtp_to": [e.strip() for e in smtp_to.split(",") if e.strip()] if smtp_to else [],
            "smtp_tls": smtp_tls,
            "send_zip_password": send_zip_password,
            "dedup_enabled": dedup_enabled,
            "webhook_url": webhook_url,
            "webhook_auth_header": webhook_auth_header,
            "heartbeat_url": heartbeat_url,
            "heartbeat_timeout": max(1, heartbeat_timeout),
            "preflight_enabled": preflight_enabled,
            "preflight_expected_mount": preflight_expected_mount,
            "preflight_expected_label": preflight_expected_label,
            "preflight_expected_uuid": preflight_expected_uuid,
            "preflight_expected_fs_type": preflight_expected_fs_type,
            "preflight_expected_owner": preflight_expected_owner,
            "preflight_auto_mount": preflight_auto_mount,
            "preflight_auto_fix_ownership": preflight_auto_fix_ownership,
            "preflight_ensure_fstab": preflight_ensure_fstab,
            "preflight_staleness_factor": max(1.0, preflight_staleness_factor),
            "preflight_local_mail_to": preflight_local_mail_to,
            "preflight_status_sentinel": preflight_status_sentinel,
            "tailscale_enabled": tailscale_enabled,
            "tailscale_auth_key": tailscale_auth_key,
            "tailscale_hostname": tailscale_hostname,
            "tailscale_advertise_tags": tailscale_advertise_tags,
            "tailscale_accept_routes": tailscale_accept_routes,
            "tailscale_disconnect_after": tailscale_disconnect_after,
        }

        # Resolve relative paths to absolute
        if config_vars["source_dir"]:
            config_vars["source_dir"] = str(Path(config_vars["source_dir"]).resolve())
        config_vars["backup_dirs"] = [str(Path(d).resolve()) for d in config_vars["backup_dirs"]]

        # Parse receiver emails
        if raw_receiver_emails:
            config_vars["receiver_emails"] = [
                email.strip() for email in raw_receiver_emails.split(",") if email.strip()
            ]

        if show:
            print("Current Configuration:\n")
            print("DEFAULT:")
            print(f"  Source Directory  : {config_vars['source_dir']}")
            print(f"  Mode             : {config_vars['mode']}")
            print(f"  Compress Type    : {config_vars['compress_type']}")
            print(
                f"  Exclude Patterns : {', '.join(config_vars['exclude_patterns']) if config_vars['exclude_patterns'] else 'None'}"
            )
            print(f"  Parallel Copies  : {config_vars['parallel_copies']}\n")

            print("BACKUPS:")
            print(f"  Backup Directories: {', '.join(config_vars['backup_dirs'])}\n")

            print("SSH:")
            print(
                f"  SSH Servers      : {', '.join(config_vars['ssh_servers']) if config_vars['ssh_servers'] else 'Not Set'}"
            )
            print(f"  SSH Username     : {config_vars['ssh_username'] or 'Not Set'}")
            # Fixed mask — never reveal the password length
            print(f"  SSH Password     : {'*****' if config_vars['ssh_password'] else 'Not Set'}")
            print(f"  SSH Key File     : {config_vars['ssh_key_file'] or 'Not Set'}")
            print(f"  Strict Host Keys : {config_vars['ssh_strict_host_key_checking']}")
            print(
                f"  Bandwidth Limit  : {config_vars['bandwidth_limit']} KB/s\n"
                if config_vars["bandwidth_limit"]
                else "  Bandwidth Limit  : Unlimited\n"
            )

            print("TAILSCALE:")
            print(f"  Enabled          : {'Yes' if config_vars['tailscale_enabled'] else 'No'}")
            print(
                f"  Auth Key         : {'*' * 8 + '...' if config_vars['tailscale_auth_key'] else 'Not Set'}"
            )
            print(f"  Hostname         : {config_vars['tailscale_hostname'] or 'Default'}")
            print(f"  Advertise Tags   : {config_vars['tailscale_advertise_tags'] or 'None'}")
            print(f"  Accept Routes    : {'Yes' if config_vars['tailscale_accept_routes'] else 'No'}")
            print(f"  Disconnect After : {'Yes' if config_vars['tailscale_disconnect_after'] else 'No'}\n")

            print("S3:")
            print(f"  Bucket  : {config_vars['s3_bucket'] or 'Not Set'}")
            print(f"  Prefix  : {config_vars['s3_prefix'] or '/'}")
            print(f"  Region  : {config_vars['s3_region'] or 'Not Set'}\n")

            print("SCHEDULE:")
            print(
                f"  Times          : {', '.join(config_vars['schedule_times']) if config_vars['schedule_times'] else 'Not Set'}"
            )
            print(f"  Interval (min) : {config_vars['interval_minutes']}\n")

            print("MODES:")
            print(f"  Local Backup : {'Enabled' if config_vars['local_mode'] else 'Disabled'}")
            print(f"  SSH Backup   : {'Enabled' if config_vars['ssh_mode'] else 'Disabled'}")
            print(f"  S3 Backup    : {'Enabled' if config_vars['s3_mode'] else 'Disabled'}")
            print(f"  DB Backup    : {'Enabled' if config_vars['db_mode'] else 'Disabled'}\n")

            print("HOOKS:")
            print(f"  Pre-Backup  : {config_vars['pre_backup_hook'] or 'Not Set'}")
            print(f"  Post-Backup : {config_vars['post_backup_hook'] or 'Not Set'}\n")

            print("RETENTION:")
            print(f"  Max Age (days) : {config_vars['max_age_days'] or 'Disabled'}")
            print(f"  Max Count      : {config_vars['max_count'] or 'Unlimited'}\n")

            print("ENCRYPTION:")
            print(f"  Enabled    : {'Yes' if config_vars['encryption_enabled'] else 'No'}")
            print(f"  Key File   : {config_vars['encryption_key_file'] or 'Not Set'}")
            print(f"  Passphrase : {'*****' if config_vars['encryption_passphrase'] else 'Not Set'}")
            print(f"  Workers    : {config_vars['encryption_workers']}\n")

            print("DATABASE:")
            print(f"  User     : {config_vars['db_user'] or 'Not Set'}")
            print(f"  Password : {'*****' if config_vars['db_password'] else 'Not Set'}")
            print(f"  Database : {config_vars['db_database'] or 'Not Set'}")
            print(f"  Host     : {config_vars['db_host']}")
            print(f"  Port     : {config_vars['db_port']}")
            print(f"  SingleTx : {'Yes' if config_vars['db_single_transaction'] else 'No'}")
            print(f"  Binlog   : {'Yes' if config_vars['db_binlog_position'] else 'No'}\n")

            print("SMTP:")
            print(f"  Host     : {config_vars['smtp_host'] or 'Not Set'}")
            print(f"  Port     : {config_vars['smtp_port']}")
            print(f"  User     : {config_vars['smtp_user'] or 'Not Set'}")
            print(f"  From     : {config_vars['smtp_from'] or 'Not Set'}")
            print(
                f"  To       : {', '.join(config_vars['smtp_to']) if config_vars['smtp_to'] else 'Not Set'}"
            )
            print(f"  TLS      : {'Yes' if config_vars['smtp_tls'] else 'No'}\n")

            print("DEDUP:")
            print(f"  Enabled  : {'Yes' if config_vars['dedup_enabled'] else 'No'}\n")

            print("WEBHOOK:")
            print(f"  URL          : {config_vars['webhook_url'] or 'Not Set'}")
            print(f"  Auth Header  : {'Set' if config_vars['webhook_auth_header'] else 'Not Set'}\n")

            print("HEARTBEAT:")
            print(f"  URL          : {config_vars['heartbeat_url'] or 'Not Set'}")
            print(f"  Timeout (s)  : {config_vars['heartbeat_timeout']}\n")

            print("PREFLIGHT:")
            print(f"  Enabled            : {'Yes' if config_vars['preflight_enabled'] else 'No'}")
            print(f"  Expected mount     : {config_vars['preflight_expected_mount'] or 'Not Set'}")
            print(f"  Expected label     : {config_vars['preflight_expected_label'] or 'Not Set'}")
            print(f"  Expected UUID      : {config_vars['preflight_expected_uuid'] or 'Not Set'}")
            print(f"  Expected fs_type   : {config_vars['preflight_expected_fs_type']}")
            print(f"  Expected owner     : {config_vars['preflight_expected_owner'] or 'Not Set'}")
            print(f"  Auto mount         : {'Yes' if config_vars['preflight_auto_mount'] else 'No'}")
            print(f"  Auto fix ownership : {'Yes' if config_vars['preflight_auto_fix_ownership'] else 'No'}")
            print(f"  Ensure fstab       : {'Yes' if config_vars['preflight_ensure_fstab'] else 'No'}")
            print(f"  Staleness factor   : {config_vars['preflight_staleness_factor']}x")
            print(f"  Local mail to      : {config_vars['preflight_local_mail_to'] or 'Not Set'}")
            print(f"  Status sentinel    : {config_vars['preflight_status_sentinel']}\n")

            print("NOTIFICATIONS:")
            print(f"  Bot             : {'Enabled' if config_vars['bot'] else 'Disabled'}")
            print(
                f"  Receiver Emails : {', '.join(config_vars['receiver_emails']) if config_vars['receiver_emails'] else 'Disabled'}\n"
            )
        else:
            return config_vars

    except Exception as e:
        logger.error(f"Error extracting config values: {e}")
        raise


# ─── Configuration Validation ───────────────────────────────────────────────


def validate_config(logger, config, require_schedule=False):
    """
    Validate that all required configuration fields are set and well-formed.

    Collects all errors before exiting so users can fix multiple issues in
    one pass. Conditionally validates SSH, S3, encryption, and database
    fields only when their respective mode is enabled.

    Parameters:
        logger: Logger instance.
        config (configparser.ConfigParser): Loaded configuration object.
        require_schedule (bool): If True, validate schedule time format.
    """
    errors = []

    # Always required: source_dir, mode, backup_dirs
    if not normalize_none(config.get("DEFAULT", "source_dir", fallback=None)):
        errors.append("Config error: 'source_dir' is not set in [DEFAULT]. Set it in config/config.ini")

    mode = normalize_none(config.get("DEFAULT", "mode", fallback=None))
    if not mode:
        errors.append("Config error: 'mode' is not set in [DEFAULT]. Set it in config/config.ini")
    elif mode not in ("full", "incremental", "differential"):
        errors.append(
            f"Config error: 'mode' in [DEFAULT] must be full, incremental, or differential, got '{mode}'"
        )

    if not config.get("BACKUPS", "backup_dirs", fallback="").strip():
        errors.append("Config error: 'backup_dirs' is not set in [BACKUPS]. Set it in config/config.ini")

    # Validate compress_type if set
    compress_type = normalize_none(config.get("DEFAULT", "compress_type", fallback=None))
    valid_compress = ("none", "zip", "zip_pw")
    if compress_type and compress_type not in valid_compress:
        errors.append(
            f"Config error: 'compress_type' in [DEFAULT] must be one of {valid_compress}, got '{compress_type}'"
        )

    # Validate SSH fields only when MODES.ssh = True
    ssh_enabled = False
    if "MODES" in config:
        try:
            ssh_enabled = config.getboolean("MODES", "ssh", fallback=False)
        except ValueError:
            errors.append("Config error: 'ssh' in [MODES] must be True or False")

    if ssh_enabled:
        if not normalize_none(config.get("SSH", "ssh_servers", fallback=None)):
            errors.append(
                "Config error: 'ssh_servers' is not set in [SSH]. Required when ssh mode is enabled"
            )
        if not normalize_none(config.get("SSH", "username", fallback=None)):
            errors.append("Config error: 'username' is not set in [SSH]. Required when ssh mode is enabled")
        if not normalize_none(config.get("SSH", "password", fallback=None)) and not normalize_none(
            config.get("SSH", "key_file", fallback=None)
        ):
            errors.append(
                "Config error: neither 'key_file' nor 'password' is set in [SSH]. "
                "One is required when ssh mode is enabled"
            )

    # Validate S3 fields only when MODES.s3 = True
    s3_enabled = False
    if "MODES" in config:
        try:
            s3_enabled = config.getboolean("MODES", "s3", fallback=False)
        except ValueError:
            errors.append("Config error: 's3' in [MODES] must be True or False")

    if s3_enabled:
        if not normalize_none(config.get("S3", "bucket", fallback=None)):
            errors.append("Config error: 'bucket' is not set in [S3]. Required when s3 mode is enabled")
        if not normalize_none(config.get("S3", "region", fallback=None)):
            errors.append("Config error: 'region' is not set in [S3]. Required when s3 mode is enabled")
        sse = normalize_none(config.get("S3", "server_side_encryption", fallback=None))
        if sse and sse not in ("AES256", "aws:kms"):
            errors.append("Config error: 'server_side_encryption' in [S3] must be AES256, aws:kms or None")

    # Validate encryption: when enabled, require either key_file or passphrase
    encryption_enabled = False
    try:
        encryption_enabled = config.getboolean("ENCRYPTION", "enabled", fallback=False)
    except ValueError:
        errors.append("Config error: 'enabled' in [ENCRYPTION] must be True or False")

    if encryption_enabled:
        has_key_file = normalize_none(config.get("ENCRYPTION", "key_file", fallback=None))
        has_passphrase = normalize_none(config.get("ENCRYPTION", "passphrase", fallback=None))
        if not has_key_file and not has_passphrase:
            errors.append(
                "Config error: [ENCRYPTION] is enabled but neither 'key_file' nor 'passphrase' is set"
            )

    # Validate database fields only when MODES.db = True
    db_enabled = False
    if "MODES" in config:
        try:
            db_enabled = config.getboolean("MODES", "db", fallback=False)
        except ValueError:
            errors.append("Config error: 'db' in [MODES] must be True or False")

    if db_enabled:
        if not normalize_none(config.get("DATABASE", "user", fallback=None)):
            errors.append("Config error: 'user' is not set in [DATABASE]. Required when db mode is enabled")
        if not normalize_none(config.get("DATABASE", "password", fallback=None)):
            errors.append(
                "Config error: 'password' is not set in [DATABASE]. Required when db mode is enabled"
            )
        if not normalize_none(config.get("DATABASE", "database", fallback=None)):
            errors.append(
                "Config error: 'database' is not set in [DATABASE]. Required when db mode is enabled"
            )

    # Validate schedule only when --scheduled is used
    if require_schedule:
        schedule_times = normalize_none(config.get("SCHEDULE", "times", fallback=None))
        if not schedule_times:
            errors.append("Config error: 'times' is not set in [SCHEDULE]. Required for --scheduled mode")
        else:
            for t in schedule_times.split(","):
                t = t.strip()
                if t and not is_valid_time_format(t):
                    errors.append(
                        f"Config error: Invalid time format '{t}' in [SCHEDULE]. Use HH:MM (24-hour)"
                    )

        if config.has_option("SCHEDULE", "interval_minutes"):
            try:
                interval = config.getint("SCHEDULE", "interval_minutes")
                if interval <= 0:
                    errors.append("Config error: 'interval_minutes' in [SCHEDULE] must be a positive integer")
            except ValueError:
                errors.append("Config error: 'interval_minutes' in [SCHEDULE] must be a valid integer")

    # Validate MODES values
    if "MODES" in config:
        for key in ("local", "ssh", "s3", "db"):
            val = config.get("MODES", key, fallback=None)
            if val is not None and val not in ("True", "False", "true", "false"):
                errors.append(f"Config error: '{key}' in [MODES] must be True or False, got '{val}'")

    # Validate email format when receiver_emails is set
    raw_emails = normalize_none(config.get("NOTIFICATIONS", "receiver_emails", fallback=None))
    if raw_emails:
        for email in raw_emails.split(","):
            email = email.strip()
            if email and not is_valid_email(email):
                errors.append(
                    f"Config error: Invalid email address '{email}' in [NOTIFICATIONS].receiver_emails"
                )

    if errors:
        for err in errors:
            logger.error(err)
        print("\nConfiguration errors found:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)


# ─── Utility Functions ──────────────────────────────────────────────────────


def is_valid_time_format(time_string):
    """
    Check if a time string is in HH:MM 24-hour format.

    Returns:
        bool: True if valid, False otherwise.
    """
    from datetime import datetime

    try:
        datetime.strptime(time_string, "%H:%M")
        return True
    except ValueError:
        return False


def load_config(logger, config_path):
    """
    Load and parse an INI configuration file.

    Exits with a clear error message if the file is empty, malformed,
    or cannot be read.

    Parameters:
        logger: Logger instance.
        config_path (str): Path to the INI file.

    Returns:
        configparser.ConfigParser: Loaded configuration object.
    """
    config = configparser.ConfigParser()

    try:
        config.read(config_path)

        if not config.sections():
            raise configparser.Error(f"Config file '{config_path}' is empty or not correctly formatted.")

        logger.info(f"Configuration loaded successfully from {config_path}")
        return config

    except configparser.Error as e:
        logger.error(f"ConfigParser error while reading file '{config_path}': {e}")
        sys.exit(1)

    except Exception as e:
        logger.error(f"Unexpected error while loading config file '{config_path}': {e}")
        sys.exit(1)
