import os
import shutil
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import paramiko
from retrying import retry
from tqdm import tqdm

from .compression import compress_directory
from .email_notify import send_email
from .encryption import remote_name, upload_source
from .utils import calculate_checksum, copy_and_verify, generate_otp, handle_symlink, should_exclude


def sync_directories_with_progress(
    logger,
    source_dirs,
    backup_dirs,
    compress=None,
    bot=None,
    receiver_emails=None,
    exclude_patterns=None,
    manifest=None,
    parallel_copies=1,
    send_zip_password=True,
):
    """
    Sync files from source directories to backup directories with progress tracking.

    Parameters:
    - logger: Logger instance for logging output.
    - source_dirs (list of str): List of source directories.
    - backup_dirs (list of str): List of backup directories.
    - compress (str, optional): If 'zip', compress the final backup directory. If 'zip_pw', compress with a password.
    - bot (TelegramBot, optional): Telegram bot instance for sending notifications.
    - receiver_emails (list of str, optional): List of emails to notify after backup.
    - exclude_patterns (list of str, optional): Glob patterns to exclude.
    - manifest (BackupManifest, optional): Manifest to record file operations.
    - parallel_copies (int, optional): Number of parallel copy threads (default 1 = sequential).
    """
    for src_dir in source_dirs:
        # List all files in the current source directory
        files = [
            f
            for f in Path(src_dir).rglob("*")
            if not f.is_dir() and not should_exclude(f.relative_to(src_dir), exclude_patterns)
        ]

        for backup_dir in backup_dirs:
            if parallel_copies > 1:
                _sync_parallel(logger, files, src_dir, backup_dir, manifest, parallel_copies)
            else:
                _sync_sequential(logger, files, src_dir, backup_dir, manifest)

    # Compress the backup directories if the compress flag is set
    if compress in ["zip", "zip_pw"]:
        password = generate_otp() if compress == "zip_pw" else None
        compress_directory(
            logger,
            src_dirs=source_dirs,
            output_dirs=backup_dirs,
            password=password,
            bot_handler=bot,
            receiver_emails=receiver_emails,
            send_password=send_zip_password,
        )

    # Send notification if bot is enabled
    if bot:
        try:
            bot.send_notification(f"Backup completed for {source_dirs}")
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    # Send email notifications if email addresses are provided
    if receiver_emails:
        subject = "Backup Completed"
        body = f"Backup completed for {source_dirs}"
        try:
            send_email(receiver_emails, subject, body, logger=logger)
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f"Failed to send email notification: {e}")


def _sync_sequential(logger, files, src_dir, backup_dir, manifest):
    """Sync files sequentially with a progress bar."""
    for file in tqdm(files, desc=f"Syncing Files from {src_dir} to {backup_dir}", unit="files"):
        _copy_single_file(logger, file, src_dir, backup_dir, manifest)


def _sync_parallel(logger, files, src_dir, backup_dir, manifest, parallel_copies):
    """Sync files in parallel using a thread pool with a thread-safe progress bar."""
    progress_lock = threading.Lock()
    pbar = tqdm(total=len(files), desc=f"Syncing Files from {src_dir} to {backup_dir}", unit="files")

    def copy_task(file):
        _copy_single_file(logger, file, src_dir, backup_dir, manifest)
        with progress_lock:
            pbar.update(1)

    with ThreadPoolExecutor(max_workers=parallel_copies) as executor:
        futures = [executor.submit(copy_task, f) for f in files]
        for future in futures:
            try:
                future.result()
            except Exception as e:
                logger.error(f"Parallel copy error: {e}")

    pbar.close()


def _copy_single_file(logger, file, src_dir, backup_dir, manifest):
    """Copy a single file from source to backup, with optional manifest recording."""
    backup_file = Path(backup_dir) / file.relative_to(src_dir)

    try:
        # Ensure the destination directory exists
        backup_file.parent.mkdir(parents=True, exist_ok=True)

        # Handle symlinks separately
        if file.is_symlink():
            handle_symlink(logger, str(file), str(backup_file))
            if manifest:
                manifest.record_copy(str(file), file.stat().st_size if file.exists() else 0)
            return

        checksum = copy_and_verify(file, backup_file)
        if checksum:
            (
                logger.info(f"Successfully backed up {file} to {backup_file}")
                if logger
                else print(f"Successfully backed up {file} to {backup_file}")
            )
            if manifest:
                manifest.record_copy(str(file), file.stat().st_size, checksum=checksum)
        else:
            (
                logger.error(f"Checksum verification failed for {file}")
                if logger
                else print(f"Checksum verification failed for {file}")
            )
            if manifest:
                manifest.record_failure(str(file), "Checksum verification failed")
    except (OSError, shutil.Error) as e:
        logger.error(f"Failed to backup {file} to {backup_file}: {e}")
        if manifest:
            manifest.record_failure(str(file), str(e))


def make_ssh_client(strict_host_key_checking=False, known_hosts_file=None):
    """
    Build a paramiko client with host-key verification.

    Keys in the system/user known_hosts (and ``known_hosts_file``) are always
    enforced — a changed key raises ``BadHostKeyException``. Unknown hosts
    are rejected when ``strict_host_key_checking`` is set; otherwise they are
    accepted with a warning (trust on first use, not persisted).
    """
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    if known_hosts_file:
        ssh.load_host_keys(os.path.expanduser(known_hosts_file))
    if strict_host_key_checking:
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())  # noqa: S507  # nosec B507
    return ssh


def _sftp_put_throttled(sftp, local_path, remote_path, bandwidth_limit_kbps):
    """
    Upload a file via SFTP with bandwidth throttling.

    Parameters:
    - sftp: paramiko SFTP client.
    - local_path (str): Local file path.
    - remote_path (str): Remote file path.
    - bandwidth_limit_kbps (int): Max transfer speed in KB/s. 0 = unlimited.
    """
    if bandwidth_limit_kbps <= 0:
        sftp.put(str(local_path), remote_path)
        return

    chunk_size = 32768  # 32KB chunks
    bytes_per_second = bandwidth_limit_kbps * 1024

    with open(local_path, "rb") as local_file, sftp.open(remote_path, "wb") as remote_file:
        remote_file.set_pipelined(True)
        while True:
            start = time.monotonic()
            data = local_file.read(chunk_size)
            if not data:
                break
            remote_file.write(data)
            # Pace the transfer
            elapsed = time.monotonic() - start
            expected = len(data) / bytes_per_second
            if elapsed < expected:
                time.sleep(expected - elapsed)


def _sftp_upload_directory(
    sftp,
    local_path,
    remote_path,
    mode="full",
    logger=None,
    exclude_patterns=None,
    manifest=None,
    bandwidth_limit=0,
    encryption_key=None,
):
    """
    Upload a local directory to a remote server via SFTP.

    Parameters:
    - sftp: paramiko SFTP client.
    - local_path (str): Local directory path.
    - remote_path (str): Remote directory path.
    - mode (str): Backup mode ('full', 'incremental', 'differential').
    - logger: Logger instance.
    - exclude_patterns (list of str, optional): Glob patterns to exclude.
    - manifest (BackupManifest, optional): Manifest to record file operations.
    - bandwidth_limit (int): Bandwidth limit in KB/s (0 = unlimited).
    - encryption_key (EncryptionKey, optional): Encrypt each file client-side
      and upload it as ``<name>.enc``.
    """
    local_path = Path(local_path)
    files = [
        f
        for f in local_path.rglob("*")
        if f.is_file() and not should_exclude(f.relative_to(local_path), exclude_patterns)
    ]

    failed = 0
    for local_file in tqdm(files, desc=f"Uploading to {remote_path}", unit="files"):
        relative = local_file.relative_to(local_path)
        remote_file = f"{remote_path}/{remote_name(relative.as_posix(), encryption_key)}"
        remote_dir = f"{remote_path}/{relative.parent}"

        # Ensure remote directory exists
        _sftp_mkdirs(sftp, remote_dir, logger=logger)

        should_upload = True
        if mode in ("incremental", "differential"):
            try:
                remote_stat = sftp.stat(remote_file)
                local_mtime = local_file.stat().st_mtime
                remote_mtime = remote_stat.st_mtime
                if (mode == "incremental" and remote_stat) or mode == "differential":
                    # Only upload if local is newer
                    should_upload = local_mtime > remote_mtime
                if not should_upload and manifest:
                    manifest.record_skip(str(local_file))
            except FileNotFoundError:
                should_upload = True  # File doesn't exist remotely

        if should_upload:
            try:
                with upload_source(local_file, encryption_key) as src:
                    _sftp_put_throttled(sftp, str(src), remote_file, bandwidth_limit)
                if logger:
                    logger.info(f"Uploaded {local_file} -> {remote_file}")
                if manifest:
                    checksum = calculate_checksum(str(local_file))
                    manifest.record_copy(str(local_file), local_file.stat().st_size, checksum=checksum)
            except Exception as e:
                failed += 1
                if logger:
                    logger.error(f"Failed to upload {local_file}: {e}")
                if manifest:
                    manifest.record_failure(str(local_file), str(e))

    # In full mode, remove remote files not present locally
    if mode == "full":
        _sftp_cleanup_extra_files(sftp, local_path, remote_path, logger, encryption_key=encryption_key)
    return failed


def _sftp_mkdirs(sftp, remote_dir, logger=None):
    """Recursively create remote directories."""
    dirs_to_create = []
    current = remote_dir
    while current and current != "/":
        try:
            sftp.stat(current)
            break
        except FileNotFoundError:
            dirs_to_create.append(current)
            current = os.path.dirname(current)
    for d in reversed(dirs_to_create):
        try:
            sftp.mkdir(d)
        except OSError as e:
            # Ignore "already exists" (errno 13 on some SFTP servers), raise others
            try:
                sftp.stat(d)
            except FileNotFoundError:
                if logger:
                    logger.error(f"Failed to create remote directory '{d}': {e}")
                raise


def _sftp_cleanup_extra_files(sftp, local_path, remote_path, logger=None, encryption_key=None):
    """Remove remote files that don't exist in the local source (full sync)."""
    local_files = set()
    for f in local_path.rglob("*"):
        if f.is_file():
            local_files.add(remote_name(f.relative_to(local_path).as_posix(), encryption_key))

    def _walk_remote(path):
        try:
            entries = sftp.listdir_attr(path)
        except Exception:
            return
        for entry in entries:
            remote_entry = f"{path}/{entry.filename}"
            if stat.S_ISDIR(entry.st_mode):
                _walk_remote(remote_entry)
            else:
                relative = os.path.relpath(remote_entry, remote_path)
                if relative not in local_files:
                    try:
                        sftp.remove(remote_entry)
                        if logger:
                            logger.info(f"Removed extra remote file: {remote_entry}")
                    except Exception as e:
                        if logger:
                            logger.error(f"Failed to remove remote file {remote_entry}: {e}")

    _walk_remote(remote_path)


@retry(stop_max_attempt_number=3, wait_fixed=2000)
def sync_ssh_server(
    source_dir,
    server,
    username,
    password=None,
    key_filepath=None,
    mode="full",
    logger=None,
    bot=None,
    exclude_patterns=None,
    manifest=None,
    bandwidth_limit=0,
    encryption_key=None,
    strict_host_key_checking=False,
    known_hosts_file=None,
):
    """
    Sync a local directory to a remote server via SSH using SFTP, with retry logic.

    Parameters:
    - source_dir (str): Local directory to sync.
    - server (str): Remote SSH server address.
    - username (str): SSH username.
    - password (str, optional): SSH password (if not using a private key).
    - key_filepath (str, optional): Path to private key (if not using password).
    - mode (str, optional): Backup mode ('full', 'incremental', 'differential').
    - logger (logging.Logger, optional): Logger instance for logging.
    - bot (TelegramBot, optional): Bot instance for sending notifications.
    - exclude_patterns (list of str, optional): Glob patterns to exclude.
    - manifest (BackupManifest, optional): Manifest to record file operations.
    - bandwidth_limit (int): Bandwidth limit in KB/s (0 = unlimited).
    - encryption_key (EncryptionKey, optional): Client-side encryption key.
    - strict_host_key_checking (bool): Refuse hosts missing from known_hosts.
    - known_hosts_file (str, optional): Extra known_hosts file to trust.
    """
    if logger:
        logger.info(f"Syncing {source_dir} to SSH server: {server} in {mode} mode")

    ssh = make_ssh_client(strict_host_key_checking, known_hosts_file)

    try:
        # Connect to SSH server using password or private key
        ssh.connect(hostname=server, username=username, password=password, key_filename=key_filepath)

        if logger:
            logger.info(f"Connected to SSH server: {server}")

        # Use SFTP to upload files
        sftp = ssh.open_sftp()
        try:
            remote_path = source_dir  # Mirror the local path on the remote
            failed = _sftp_upload_directory(
                sftp,
                source_dir,
                remote_path,
                mode=mode,
                logger=logger,
                exclude_patterns=exclude_patterns,
                manifest=manifest,
                bandwidth_limit=bandwidth_limit,
                encryption_key=encryption_key,
            )
        finally:
            sftp.close()

        if failed:
            raise RuntimeError(f"{failed} file(s) failed to upload to {server}")
        if logger:
            logger.info(f"Completed SSH sync to {server}")

    except Exception as e:
        if logger:
            logger.error(f"Failed to sync to SSH server {server}: {e}")
        raise

    finally:
        ssh.close()
        if logger:
            logger.info(f"Closed connection to SSH server: {server}")
        else:
            print(f"Closed connection to SSH server: {server}")


def sync_ssh_servers_concurrently(
    source_dir,
    ssh_servers,
    username,
    password=None,
    key_filepath=None,
    mode="full",
    logger=None,
    bot=None,
    receiver_emails=None,
    exclude_patterns=None,
    manifest=None,
    bandwidth_limit=0,
    encryption_key=None,
    strict_host_key_checking=False,
    known_hosts_file=None,
):
    """
    Sync a local directory to multiple SSH servers concurrently.

    Parameters:
    - source_dir (str): Local directory to sync.
    - ssh_servers (list of str): List of SSH server addresses.
    - username (str): SSH username for all servers.
    - password (str, optional): SSH password (if not using a key).
    - key_filepath (str, optional): Path to private key.
    - mode (str, optional): Backup mode ('full', 'incremental', 'differential').
    - logger (logging.Logger, optional): Logger instance for logging.
    - bot (TelegramBot, optional): Bot instance for notifications.
    - receiver_emails (list of str, optional): List of email addresses for notifications.
    - exclude_patterns (list of str, optional): Glob patterns to exclude.
    - manifest (BackupManifest, optional): Manifest to record file operations.
    - bandwidth_limit (int): Bandwidth limit in KB/s (0 = unlimited).

    Returns:
    - list of str: Servers that failed (empty when all succeeded).
    """

    def sync_ssh_server_task(server):
        try:
            sync_ssh_server(
                source_dir,
                server,
                username,
                password,
                key_filepath,
                mode,
                logger=logger,
                bot=bot,
                exclude_patterns=exclude_patterns,
                manifest=manifest,
                bandwidth_limit=bandwidth_limit,
                encryption_key=encryption_key,
                strict_host_key_checking=strict_host_key_checking,
                known_hosts_file=known_hosts_file,
            )
            return None
        except Exception as e:
            if logger:
                logger.error(f"Failed to sync to SSH server {server}: {e}")
            else:
                print(e)
            return server

    # Execute SSH syncs concurrently with capped thread pool
    max_workers = min(len(ssh_servers), 10)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(sync_ssh_server_task, ssh_servers))
    failed_servers = [server for server in results if server]

    # Notifications
    if bot:
        bot.send_notification("Completed concurrent SSH sync")
    if receiver_emails:
        send_email(receiver_emails, "Backup Completed", f"Backup completed for {source_dir}", logger=logger)
    return failed_servers


def perform_full_backup(
    logger,
    source_dir,
    backup_dirs,
    compress=None,
    bot=None,
    receiver_emails=None,
    exclude_patterns=None,
    manifest=None,
    parallel_copies=1,
    send_zip_password=True,
):
    """
    Perform a full backup of the source directory to the backup directories.
    """
    logger.info(f"Performing full backup from {source_dir}")
    if isinstance(source_dir, str):
        source_dir = [source_dir]
    sync_directories_with_progress(
        logger,
        source_dir,
        backup_dirs,
        compress=compress,
        bot=bot,
        receiver_emails=receiver_emails,
        exclude_patterns=exclude_patterns,
        manifest=manifest,
        parallel_copies=parallel_copies,
        send_zip_password=send_zip_password,
    )


def perform_incremental_backup(
    logger,
    source_dir,
    backup_dirs,
    last_backup_time,
    bot=None,
    receiver_emails=None,
    exclude_patterns=None,
    manifest=None,
):
    """
    Perform an incremental backup of the source directory to the backup directories.
    """
    logger.info(f"Performing incremental backup from {source_dir} since last backup time: {last_backup_time}")
    files = [
        f
        for f in Path(source_dir).rglob("*")
        if not f.is_dir() and not should_exclude(f.relative_to(source_dir), exclude_patterns)
    ]

    failed_count = 0
    for file in tqdm(files, desc="Syncing Incremental Files", unit="files"):
        file_mtime = os.path.getmtime(file)
        for backup_dir in backup_dirs:
            backup_file = Path(backup_dir) / file.relative_to(source_dir)
            if file_mtime > last_backup_time or not backup_file.exists():
                try:
                    logger.info(f"Backing up modified or new file: {file} (modified at {file_mtime})")
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    if file.is_symlink():
                        handle_symlink(logger, str(file), str(backup_file))
                        if manifest:
                            manifest.record_copy(str(file), file.stat().st_size if file.exists() else 0)
                        continue
                    checksum = copy_and_verify(file, backup_file)
                    if checksum:
                        logger.info(f"Incremental backup of {file} to {backup_file}")
                        if manifest:
                            manifest.record_copy(str(file), file.stat().st_size, checksum=checksum)
                    else:
                        logger.error(f"Checksum verification failed for {file}")
                        failed_count += 1
                        if manifest:
                            manifest.record_failure(str(file), "Checksum verification failed")
                except Exception as e:
                    logger.error(f"Failed to backup {file} to {backup_file}: {e}")
                    failed_count += 1
                    if manifest:
                        manifest.record_failure(str(file), str(e))
            else:
                logger.info(f"Skipping unmodified file: {file} (modified at {file_mtime})")
                if manifest:
                    manifest.record_skip(str(file))
    if failed_count:
        logger.warning(f"Incremental backup completed with {failed_count} file error(s).")

    if bot:
        bot.send_notification(f"Completed incremental backup from {source_dir}")
    if receiver_emails:
        subject = "Backup Completed"
        body = f"Backup completed for {source_dir}"
        send_email(receiver_emails, subject, body, logger=logger)


def perform_differential_backup(
    logger,
    source_dir,
    backup_dirs,
    last_full_backup_time,
    bot=None,
    receiver_emails=None,
    exclude_patterns=None,
    manifest=None,
):
    """
    Perform a differential backup of the source directory to the backup directories.
    """
    logger.info(f"Performing differential backup from {source_dir}")
    files = [
        f
        for f in Path(source_dir).rglob("*")
        if not f.is_dir() and not should_exclude(f.relative_to(source_dir), exclude_patterns)
    ]
    failed_count = 0
    for file in tqdm(files, desc="Syncing Differential Files", unit="files"):
        if os.path.getmtime(file) > last_full_backup_time:
            for backup_dir in backup_dirs:
                backup_file = Path(backup_dir) / file.relative_to(source_dir)
                try:
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    if file.is_symlink():
                        handle_symlink(logger, str(file), str(backup_file))
                        if manifest:
                            manifest.record_copy(str(file), file.stat().st_size if file.exists() else 0)
                        continue
                    checksum = copy_and_verify(file, backup_file)
                    if checksum:
                        logger.info(f"Differential backup of {file} to {backup_file}")
                        if manifest:
                            manifest.record_copy(str(file), file.stat().st_size, checksum=checksum)
                    else:
                        logger.error(f"Checksum verification failed for {file}")
                        failed_count += 1
                        if manifest:
                            manifest.record_failure(str(file), "Checksum verification failed")
                except Exception as e:
                    logger.error(f"Failed to backup {file} to {backup_file}: {e}")
                    failed_count += 1
                    if manifest:
                        manifest.record_failure(str(file), str(e))
        else:
            if manifest:
                manifest.record_skip(str(file))
    if failed_count:
        logger.warning(f"Differential backup completed with {failed_count} file error(s).")

    if bot:
        bot.send_notification(f"Completed differential backup from {source_dir}")
    if receiver_emails:
        subject = "Backup Completed"
        body = f"Backup completed for {source_dir}"
        send_email(receiver_emails, subject, body, logger=logger)
