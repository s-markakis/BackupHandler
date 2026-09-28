"""
restore.py - Multi-Source Backup Restore Engine

Restores backup data from multiple source types:
  - Local directories  (direct file copy with SHA-256 verification)
  - ZIP archives       (standard extraction)
  - SSH/SFTP remotes   (download via paramiko, then local restore)
  - S3 buckets         (download via boto3, then local restore)

Supports encrypted backups (auto-detects ``.enc`` files and decrypts to a
temp directory), symlink preservation, and point-in-time restore using
manifest history.
"""

import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from .encryption import decrypt_directory
from .manifest import load_manifests_up_to
from .utils import verify_backup
from .verify import _find_file_in_backup

# ─── Remote Path Detection & Parsing ────────────────────────────────────────


def _is_ssh_path(path):
    """Check if a path matches SSH syntax (``user@host:/path`` or ``ssh://...``)."""
    return bool(re.match(r"^[\w.-]+@[\w.-]+:.+", path) or path.startswith("ssh://"))


def _is_s3_path(path):
    """Check if a path matches S3 URI syntax (``s3://bucket/prefix``)."""
    return path.startswith("s3://")


def _parse_ssh_path(path):
    """
    Parse an SSH path into its components.

    Supports two formats:
      - ``user@host:/remote/path``
      - ``ssh://user@host/remote/path``

    Returns:
        tuple: ``(user, host, remote_path)`` where user may be None.
    """
    if path.startswith("ssh://"):
        # ssh://user@host/path
        path = path[6:]
        if "@" in path:
            user_host, remote = path.split("/", 1) if "/" in path else (path, "")
            user, host = user_host.split("@", 1)
        else:
            user = None
            host, remote = path.split("/", 1) if "/" in path else (path, "")
        return user, host, "/" + remote
    # user@host:/path
    user_host, remote_path = path.split(":", 1)
    if "@" in user_host:
        user, host = user_host.split("@", 1)
    else:
        user, host = None, user_host
    return user, host, remote_path


def _parse_s3_path(path):
    """
    Parse an S3 URI into bucket and prefix components.

    Example: ``s3://my-bucket/backups/daily`` -> ``('my-bucket', 'backups/daily')``

    Returns:
        tuple: ``(bucket, prefix)`` where prefix may be empty.
    """
    stripped = path[5:]  # remove s3://
    if "/" in stripped:
        bucket, prefix = stripped.split("/", 1)
    else:
        bucket, prefix = stripped, ""
    return bucket, prefix


# ─── Remote Download Handlers ───────────────────────────────────────────────


def _is_within(base, candidate):
    """Return True if ``candidate`` resolves to a path inside ``base``."""
    base = Path(base).resolve()
    try:
        Path(candidate).resolve().relative_to(base)
        return True
    except ValueError:
        return False


def _download_from_ssh(
    logger,
    ssh_path,
    local_dir,
    ssh_password=None,
    strict_host_key_checking=False,
    known_hosts_file=None,
    ssh_key_file=None,
):
    """
    Download an entire remote directory via SFTP to a local directory.

    Host keys are verified via ``make_ssh_client`` (known_hosts enforced;
    unknown hosts rejected when ``strict_host_key_checking`` is set).

    Parameters:
        logger: Logger instance.
        ssh_path (str): SSH path (``user@host:/path`` or ``ssh://...``).
        local_dir (str): Local directory to download files into.
        ssh_password (str, optional): SSH password for authentication.

    Returns:
        bool: True if download completed successfully.
    """
    try:
        from .sync import make_ssh_client  # imports paramiko
    except ImportError:
        logger.error("paramiko is not installed. Install it with: pip install paramiko")
        return False

    user, host, remote_path = _parse_ssh_path(ssh_path)
    logger.info(f"Downloading from SSH: {user}@{host}:{remote_path} -> {local_dir}")

    ssh = make_ssh_client(strict_host_key_checking, known_hosts_file)

    try:
        ssh.connect(hostname=host, username=user, password=ssh_password, key_filename=ssh_key_file)
        sftp = ssh.open_sftp()

        try:
            _sftp_download_recursive(sftp, remote_path, local_dir, logger)
        finally:
            sftp.close()

        logger.info(f"SSH download complete: {remote_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to download from SSH {host}: {e}")
        return False
    finally:
        ssh.close()


def _sftp_download_recursive(sftp, remote_dir, local_dir, logger):
    """
    Recursively download all files and subdirectories from a remote SFTP path.

    Creates the local directory structure as needed and logs each file transfer.
    Individual file failures are logged but do not abort the overall download.
    """
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    try:
        entries = sftp.listdir_attr(remote_dir)
    except Exception as e:
        logger.error(f"Cannot list remote directory {remote_dir}: {e}")
        return

    for entry in entries:
        # A malicious server can return names like "../x" or "a/b".
        if entry.filename in ("", ".", "..") or "/" in entry.filename or "\\" in entry.filename:
            logger.warning(f"Skipping unsafe remote file name: {entry.filename!r}")
            continue
        remote_entry = f"{remote_dir}/{entry.filename}"
        local_entry = local_dir / entry.filename

        if stat.S_ISDIR(entry.st_mode):
            _sftp_download_recursive(sftp, remote_entry, str(local_entry), logger)
        else:
            try:
                sftp.get(remote_entry, str(local_entry))
                logger.debug(f"Downloaded: {remote_entry}")
            except Exception as e:
                logger.error(f"Failed to download {remote_entry}: {e}")


def _download_from_s3(logger, s3_path, local_dir, region=None, access_key=None, secret_key=None):
    """
    Download all objects under an S3 prefix to a local directory.

    Uses the S3 paginator API to handle buckets with more than 1,000 objects.
    The local directory structure mirrors the S3 key hierarchy relative to
    the specified prefix.

    Parameters:
        logger: Logger instance.
        s3_path (str): S3 URI (``s3://bucket/prefix``).
        local_dir (str): Local directory to download files into.
        region (str, optional): AWS region name.
        access_key (str, optional): AWS access key ID.
        secret_key (str, optional): AWS secret access key.

    Returns:
        bool: True if download completed successfully.
    """
    try:
        import boto3
    except ImportError:
        logger.error("boto3 is not installed. Install it with: pip install boto3")
        return False

    bucket, prefix = _parse_s3_path(s3_path)
    logger.info(f"Downloading from S3: s3://{bucket}/{prefix} -> {local_dir}")

    session_kwargs = {}
    if region:
        session_kwargs["region_name"] = region
    if access_key and secret_key:
        session_kwargs["aws_access_key_id"] = access_key
        session_kwargs["aws_secret_access_key"] = secret_key

    s3 = boto3.client("s3", **session_kwargs)
    local_dir = Path(local_dir)

    try:
        paginator = s3.get_paginator("list_objects_v2")
        page_kwargs = {"Bucket": bucket}
        if prefix:
            page_kwargs["Prefix"] = prefix

        downloaded = 0
        for page in paginator.paginate(**page_kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Compute relative path from prefix
                relative = key[len(prefix) :].lstrip("/") if prefix else key

                if not relative:
                    continue

                local_file = local_dir / relative
                # Object keys are attacker-influenced; never write outside local_dir.
                if Path(relative).is_absolute() or not _is_within(local_dir, local_file):
                    logger.warning(f"Skipping S3 object with unsafe key: {key!r}")
                    continue
                local_file.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, key, str(local_file))
                downloaded += 1

        logger.info(f"S3 download complete: {downloaded} files from s3://{bucket}/{prefix}")
        return True
    except Exception as e:
        logger.error(f"Failed to download from S3: {e}")
        return False


def restore_backup(
    logger,
    from_dir,
    to_dir,
    timestamp=None,
    encryption_passphrase=None,
    encryption_key_file=None,
    ssh_password=None,
    s3_region=None,
    s3_access_key=None,
    s3_secret_key=None,
    dry_run=False,
    strict_host_key_checking=False,
    known_hosts_file=None,
    ssh_key_file=None,
):
    """
    Restore files from a local, SSH, or S3 backup source.

    Parameters:
    - logger: Logger instance.
    - from_dir (str): Source — local path, user@host:/path, or s3://bucket/prefix.
    - to_dir (str): Destination directory to restore files to.
    - timestamp (str, optional): Restore to a specific point in time (YYYYMMDD_HHMMSS).
    - encryption_passphrase (str, optional): Passphrase for decrypting .enc files.
    - encryption_key_file (str, optional): Path to key file for decrypting .enc files.
    - ssh_password (str, optional): SSH password for remote restore.
    - s3_region (str, optional): AWS region for S3 restore.
    - s3_access_key (str, optional): AWS access key for S3 restore.
    - s3_secret_key (str, optional): AWS secret key for S3 restore.

    Returns:
    - bool: True if restore completed successfully, False otherwise.
    """
    to_path = Path(to_dir)

    # Dry-run: show what would be restored without modifying anything
    if dry_run:
        return _dry_run_restore(logger, from_dir, to_dir, timestamp)

    to_path.mkdir(parents=True, exist_ok=True)

    # Remote SSH restore
    if _is_ssh_path(from_dir):
        with tempfile.TemporaryDirectory() as tmp_dir:
            if not _download_from_ssh(
                logger,
                from_dir,
                tmp_dir,
                ssh_password=ssh_password,
                strict_host_key_checking=strict_host_key_checking,
                known_hosts_file=known_hosts_file,
                ssh_key_file=ssh_key_file,
            ):
                return False
            return _restore_local(
                logger, Path(tmp_dir), to_path, timestamp, encryption_passphrase, encryption_key_file
            )

    # Remote S3 restore
    if _is_s3_path(from_dir):
        with tempfile.TemporaryDirectory() as tmp_dir:
            if not _download_from_s3(
                logger,
                from_dir,
                tmp_dir,
                region=s3_region,
                access_key=s3_access_key,
                secret_key=s3_secret_key,
            ):
                return False
            return _restore_local(
                logger, Path(tmp_dir), to_path, timestamp, encryption_passphrase, encryption_key_file
            )

    # Local restore
    from_path = Path(from_dir)
    if not from_path.exists():
        logger.error(f"Restore source does not exist: {from_dir}")
        return False

    to_path.mkdir(parents=True, exist_ok=True)

    # ZIP archive restore
    if from_path.is_file() and from_path.suffix == ".zip":
        return _restore_from_zip(logger, from_path, to_path)

    # Directory restore
    if from_path.is_dir():
        # Check if backup contains encrypted files
        has_enc_files = any(from_path.rglob("*.enc"))
        if has_enc_files and (encryption_passphrase or encryption_key_file):
            logger.info("Encrypted files detected. Decrypting to temporary directory before restore.")
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                # Copy backup to temp dir to avoid modifying original
                shutil.copytree(from_path, tmp_path / "backup", dirs_exist_ok=True)
                decrypt_dir = tmp_path / "backup"
                decrypt_directory(
                    decrypt_dir, passphrase=encryption_passphrase, key_file=encryption_key_file, logger=logger
                )
                if timestamp:
                    return _restore_with_manifests(logger, decrypt_dir, to_path, timestamp)
                else:
                    return _restore_full_directory(logger, decrypt_dir, to_path)
        elif has_enc_files:
            logger.warning(
                "Encrypted files detected but no encryption passphrase or key_file configured. "
                "Encrypted files will be copied as-is."
            )

        if timestamp:
            return _restore_with_manifests(logger, from_path, to_path, timestamp)
        else:
            return _restore_full_directory(logger, from_path, to_path)

    logger.error(f"Unsupported restore source: {from_dir}")
    return False


def _dry_run_restore(logger, from_dir, to_dir, timestamp):
    """Show what a restore would do without modifying any files."""
    print("[DRY RUN] Restore preview:")
    print(f"  Source:      {from_dir}")
    print(f"  Destination: {to_dir}")

    if _is_ssh_path(from_dir):
        print("  Type:        SSH remote download + local restore")
    elif _is_s3_path(from_dir):
        print("  Type:        S3 download + local restore")
    else:
        from_path = Path(from_dir)
        if from_path.is_file() and from_path.suffix == ".zip":
            print("  Type:        ZIP archive extraction")
        elif from_path.is_dir():
            files = [
                f
                for f in from_path.rglob("*")
                if f.is_file() and not (f.name.startswith("backup_manifest_") and f.suffix == ".json")
            ]
            enc_count = sum(1 for f in files if f.suffix == ".enc")
            print("  Type:        Directory restore")
            print(f"  Files:       {len(files)}")
            if enc_count:
                print(f"  Encrypted:   {enc_count} files")
        else:
            print("  Type:        Unknown source")

    if timestamp:
        print(f"  Timestamp:   {timestamp} (point-in-time restore)")

    logger.info(f"[DRY RUN] Would restore from {from_dir} to {to_dir}")
    print("\n[DRY RUN] No files were modified.")
    return True


# ─── Local Restore Handlers ─────────────────────────────────────────────────


def _restore_local(logger, from_path, to_path, timestamp, encryption_passphrase, encryption_key_file):
    """
    Perform a local restore with automatic encrypted file detection.

    Used as the final restore step after SSH/S3 downloads. Decrypts ``.enc``
    files in-place (within the temp download directory) if encryption
    credentials are available, then delegates to full-directory or
    manifest-based restore.
    """
    has_enc_files = any(from_path.rglob("*.enc"))
    if has_enc_files and (encryption_passphrase or encryption_key_file):
        logger.info("Encrypted files detected in downloaded backup. Decrypting before restore.")
        decrypt_directory(
            from_path, passphrase=encryption_passphrase, key_file=encryption_key_file, logger=logger
        )
    elif has_enc_files:
        logger.warning("Encrypted files detected but no encryption credentials. Files will be copied as-is.")

    if timestamp:
        return _restore_with_manifests(logger, from_path, to_path, timestamp)
    else:
        return _restore_full_directory(logger, from_path, to_path)


def _restore_from_zip(logger, zip_path, to_dir):
    """
    Restore a backup from a ZIP archive by extracting all contents.

    Parameters:
        logger: Logger instance.
        zip_path (Path): Path to the ZIP archive.
        to_dir (Path): Destination directory for extraction.

    Returns:
        bool: True if extraction succeeded.
    """
    logger.info(f"Restoring from ZIP archive: {zip_path}")
    from .compression import extract_archive, get_password_by_timestamp

    password = None
    m = re.fullmatch(r"backup_(\d{8}_\d{6})\.zip", Path(zip_path).name)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            encrypted = any(info.flag_bits & 0x1 for info in zf.infolist())
        if encrypted:
            password = get_password_by_timestamp(m.group(1), logger) if m else None
            if not password:
                logger.error(f"{zip_path} is password-protected and no password was found in the keyring")
                return False
        extract_archive(zip_path, to_dir, password)
        logger.info(f"Successfully restored {zip_path} to {to_dir}")
        return True
    except zipfile.BadZipFile:
        logger.error(f"Bad ZIP file: {zip_path}")
        return False
    except Exception as e:
        logger.error(f"Failed to restore from ZIP: {e}")
        return False


def _restore_full_directory(logger, from_dir, to_dir):
    """
    Perform a full restore by copying all files from the backup to the destination.

    Each file is verified with SHA-256 checksums after copy. Symlinks are
    preserved as links rather than being dereferenced. Manifest JSON files
    are excluded from the restore.

    Parameters:
        logger: Logger instance.
        from_dir (Path): Source backup directory.
        to_dir (Path): Destination restore directory.

    Returns:
        bool: True if all files were restored without errors.
    """
    logger.info(f"Restoring full directory: {from_dir} -> {to_dir}")
    files = [
        f
        for f in from_dir.rglob("*")
        if f.is_file() and not (f.name.startswith("backup_manifest_") and f.suffix == ".json")
    ]

    copied = 0
    failed = 0

    for file in files:
        relative = file.relative_to(from_dir)
        dest_file = to_dir / relative

        try:
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if file.is_symlink():
                link_target = os.readlink(file)
                if dest_file.exists() or dest_file.is_symlink():
                    dest_file.unlink()
                os.symlink(link_target, dest_file)
                copied += 1
                continue

            shutil.copy2(file, dest_file)

            if verify_backup(file, dest_file):
                logger.info(f"Restored: {relative}")
                copied += 1
            else:
                logger.error(f"Checksum verification failed during restore: {relative}")
                failed += 1
        except Exception as e:
            logger.error(f"Failed to restore {relative}: {e}")
            failed += 1

    logger.info(f"Restore complete: {copied} files restored, {failed} failures")
    return failed == 0


def _restore_with_manifests(logger, from_dir, to_dir, timestamp):
    """
    Restore files to a specific point in time using manifest history.

    Loads all manifests up to the given timestamp in chronological order
    and builds a consolidated file list where the latest version of each
    file wins. This reconstructs the backup state as it existed at the
    specified timestamp.

    Parameters:
        logger: Logger instance.
        from_dir (Path): Source backup directory containing manifests.
        to_dir (Path): Destination restore directory.
        timestamp (str): Cutoff timestamp in YYYYMMDD_HHMMSS format.

    Returns:
        bool: True if all files were restored without errors.
    """
    logger.info(f"Restoring with manifests up to timestamp: {timestamp}")
    manifests = load_manifests_up_to(from_dir, timestamp)

    if not manifests:
        logger.warning(
            f"No manifests found up to timestamp {timestamp}. Falling back to full directory restore."
        )
        return _restore_full_directory(logger, from_dir, to_dir)

    logger.info(f"Found {len(manifests)} manifest(s) to apply")

    # Collect all files that were copied (latest version wins)
    files_to_restore = {}
    for manifest in manifests:
        for entry in manifest.get("copied", []):
            files_to_restore[entry["path"]] = entry

    copied = 0
    failed = 0

    for file_path, _entry in files_to_restore.items():
        src = Path(file_path)
        if src.is_absolute():
            # The manifest records the original source path; pick the backup
            # file whose trailing path components match it best, so files
            # sharing a name in different folders are not confused.
            src = _find_file_in_backup(from_dir, file_path) or from_dir / "__missing__"
        else:
            src = from_dir / src

        # Only restore files that live inside the backup. Without this check an
        # absolute manifest path that is not in the backup would be copied from
        # the live system (wrong data, and a way to exfiltrate arbitrary files).
        if not src.exists() or not _is_within(from_dir, src):
            logger.warning(f"Source file not found in backup for restore: {file_path}")
            failed += 1
            continue

        # Determine destination
        try:
            relative = src.relative_to(from_dir)
        except ValueError:
            relative = Path(src.name)

        dest_file = to_dir / relative

        try:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_file)

            if verify_backup(src, dest_file):
                logger.info(f"Restored: {relative}")
                copied += 1
            else:
                logger.error(f"Checksum verification failed: {relative}")
                failed += 1
        except Exception as e:
            logger.error(f"Failed to restore {relative}: {e}")
            failed += 1

    logger.info(f"Manifest-based restore complete: {copied} files restored, {failed} failures")
    return failed == 0
