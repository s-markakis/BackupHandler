"""
utils.py - Shared Utilities

Helper functions used across the backup pipeline: path filtering, hook
execution, checksum calculation, timestamp persistence,
and input validation.
"""

import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from .paths import PROJECT_ROOT as _PROJECT_ROOT

# Define file paths for storing timestamps of backups (absolute, relative to project root)
TIMESTAMP_FILE = _PROJECT_ROOT / "BackupTimestamp" / "backup_timestamp.json"
FULL_BACKUP_TIMESTAMP_FILE = _PROJECT_ROOT / "BackupTimestamp" / "full_backup_timestamp.json"


def should_exclude(file_path: os.PathLike | str, patterns: Iterable[str] | None) -> bool:
    """
    Check if a file path matches any of the exclude patterns.

    Parameters:
    - file_path (Path or str): The file path to check.
    - patterns (list of str): Glob patterns to match against (e.g., ['*.log', '__pycache__/*']).

    Returns:
    - bool: True if the file should be excluded, False otherwise.
    """
    if not patterns:
        return False
    file_path = Path(file_path)
    filename = file_path.name
    relative_str = str(file_path)
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        # Match against filename
        if fnmatch.fnmatch(filename, pattern):
            return True
        # Match against relative path
        if fnmatch.fnmatch(relative_str, pattern):
            return True
    return False


def run_hook(logger, command: str | None, hook_name: str) -> bool:
    """
    Execute a pre/post backup hook command.

    Parameters:
    - logger: Logger instance.
    - command (str): Shell command to execute.
    - hook_name (str): Name of the hook (for logging).

    Returns:
    - bool: True if the hook succeeded (exit code 0), False otherwise.
    """
    if not command or not command.strip():
        return True
    logger.info(f"Running {hook_name} hook: {command}")
    try:
        # shell=True is intentional — hooks are user-supplied commands from
        # config.ini that may legitimately contain pipes, redirects, or env
        # expansion. The config file itself is the trust boundary (root-owned,
        # not user input), so treating its contents as code is expected.
        result = subprocess.run(  # noqa: S602  # nosec B602
            command,
            shell=True,
            timeout=300,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            logger.info(f"[{hook_name}] stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            logger.warning(f"[{hook_name}] stderr: {result.stderr.strip()}")
        if result.returncode != 0:
            logger.error(f"{hook_name} hook failed with exit code {result.returncode}")
            return False
        logger.info(f"{hook_name} hook completed successfully.")
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"{hook_name} hook timed out after 300 seconds.")
        return False
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"{hook_name} hook failed: {e}")
        return False


def generate_otp(length: int = 16) -> str:
    """
    Generate a cryptographically secure one-time password (OTP).

    Uses :mod:`secrets` (CSPRNG) rather than :mod:`random`, since this OTP
    is also used as a password for ``zip_pw`` compressed archives and must
    be resistant to guessing attacks.

    Parameters:
        length: The length of the OTP in characters. Defaults to 16.

    Returns:
        A randomly generated password using ASCII letters and digits.
    """
    characters = string.ascii_letters + string.digits
    return "".join(secrets.choice(characters) for _ in range(length))


def get_last_backup_time() -> int:
    """
    Retrieve the timestamp of the last incremental backup.

    Returns:
    - int: The timestamp of the last backup, or 0 if no backup has been performed.

    Reads the timestamp from a JSON file. Defaults to epoch time if the file does not exist.
    """
    if TIMESTAMP_FILE.exists():
        with open(TIMESTAMP_FILE) as f:
            data = json.load(f)
        return data.get("last_backup_time", 0)
    else:
        return 0  # Default to epoch if no backup has been performed


def update_last_backup_time() -> None:
    """
    Update the timestamp of the last incremental backup.

    This function writes the current time (in seconds since epoch) to the JSON file.
    """
    TIMESTAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"last_backup_time": int(time.time())}
    with open(TIMESTAMP_FILE, "w") as f:
        json.dump(data, f)


def get_last_full_backup_time() -> int:
    """
    Retrieve the timestamp of the last full backup.

    Returns:
    - int: The timestamp of the last full backup, or 0 if no full backup has been performed.

    Reads the timestamp from a JSON file. Defaults to epoch time if the file does not exist.
    """
    if FULL_BACKUP_TIMESTAMP_FILE.exists():
        with open(FULL_BACKUP_TIMESTAMP_FILE) as f:
            data = json.load(f)
        return data.get("last_full_backup_time", 0)
    else:
        return 0  # Default to epoch if no full backup has been performed


def update_last_full_backup_time() -> None:
    """
    Update the timestamp of the last full backup.

    This function writes the current time (in seconds since epoch) to the JSON file.
    """
    FULL_BACKUP_TIMESTAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"last_full_backup_time": int(time.time())}
    with open(FULL_BACKUP_TIMESTAMP_FILE, "w") as f:
        json.dump(data, f)


def calculate_checksum(file_path: os.PathLike | str, logger=None) -> str | None:
    """
    Calculate the SHA-256 checksum of a file.

    Parameters:
    - file_path (str): The path to the file for which to calculate the checksum.
    - logger (logging.Logger, optional): Logger instance for logging errors.

    Returns:
    - str: The SHA-256 checksum of the file, or None if an error occurs.
    """
    hash_sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hash_sha256.update(chunk)
    except OSError as e:
        if logger:
            logger.error(f"Failed to generate checksum for {file_path}: {e}")
        return None
    return hash_sha256.hexdigest()


def verify_backup(source_file: os.PathLike | str, destination_file: os.PathLike | str) -> bool:
    """
    Verify that a backup file matches the source file by comparing checksums.

    Parameters:
    - source_file (str): The path to the source file.
    - destination_file (str): The path to the backup file.

    Returns:
    - bool: True if the checksums match, False otherwise.
    """
    return calculate_checksum(source_file) == calculate_checksum(destination_file)


def copy_and_verify(source_file: os.PathLike | str, destination_file: os.PathLike | str) -> str | None:
    """
    Copy ``source_file`` to ``destination_file`` and verify the copy.

    The source is hashed while it is being copied, then the destination is
    hashed once — two reads of the data instead of the three that
    copy + ``verify_backup`` + a separate manifest checksum would need.
    Metadata (mode, timestamps) is preserved like ``shutil.copy2``.

    Returns:
        The SHA-256 checksum of the source if the copy verified, else None.
    """
    src_hash = hashlib.sha256()
    with open(source_file, "rb") as src, open(destination_file, "wb") as dst:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            src_hash.update(chunk)
            dst.write(chunk)
    shutil.copystat(source_file, destination_file)
    checksum = src_hash.hexdigest()
    return checksum if calculate_checksum(destination_file) == checksum else None


def _get_backup_checksums(backup: os.PathLike | str) -> dict[str, str]:
    """
    Generate a dictionary of file checksums for all files in the backup directory.

    Parameters:
    - backup (str): The path to the backup directory.

    Returns:
    - dict: A dictionary where keys are file paths and values are their SHA-256 checksums.
    """
    checksums = {}
    for root, _dirs, files in os.walk(backup):
        for file_name in files:
            file_path = os.path.join(root, file_name)
            checksum = calculate_checksum(file_path)
            if checksum:
                checksums[file_path] = checksum
    return checksums


def handle_symlink(logger, src_path: str, dst_path: str) -> None:
    """
    Handle symbolic links by copying the link itself rather than resolving it.

    Parameters:
    - src_path (str): The path to the source symbolic link.
    - dst_path (str): The path where the new symbolic link will be created.
    """
    try:
        os.symlink(os.readlink(src_path), dst_path)
        logger.info(f"Created symlink '{dst_path}' pointing to '{os.readlink(src_path)}'")
    except OSError as e:
        logger.error(f"Failed to create symlink '{dst_path}': {e}")


def validate_directories(logger, source_dir: str, backup_dir: str) -> None:
    """
    Validate the existence of the source and backup directories.

    Parameters:
    - source_dir (str): The path to the source directory.
    - backup_dir (str): The path to the backup directory.
    """
    try:
        if not os.path.exists(source_dir):
            raise FileNotFoundError(f"Source directory '{source_dir}' does not exist.")

        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
            logger.info(f"Backup directory '{backup_dir}' created.")
        else:
            logger.info(f"Backup directory '{backup_dir}' exists.")
    except OSError as e:
        logger.error(f"Error validating directories: {e}")
        raise


def list_files_in_directory(directory: os.PathLike | str) -> list[Path]:
    """
    List all files in a directory and its subdirectories.

    Parameters:
    - directory (str): The path to the directory.

    Returns:
    - List[Path]: A list of file paths in the directory.
    """
    return [f for f in Path(directory).rglob("*") if f.is_file()]


def is_valid_email(email: str) -> bool:
    """
    Check if the provided email address is valid.

    Parameters:
    - email (str): The email address to validate.

    Returns:
    - bool: True if the email is valid, False otherwise.
    """
    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(email_pattern, email) is not None
