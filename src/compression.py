"""
compression.py - ZIP Archiving with Optional AES-256 Password Protection

Archives source directories into ``backup_<timestamp>.zip`` files. With a
password, archives use WinZip AES-256 (via ``pyzipper``) instead of the
legacy ZipCrypto scheme, which is trivially breakable. Relative paths are
preserved inside the archive, and archives are written atomically.

Archive passwords are stored in the OS keyring under
``compression_service``/<timestamp>. Because keyring backends cannot list
their entries, an index of stored timestamps is kept under a reserved
username so ``clear_keyring`` can remove every password it created.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import keyring
import pyzipper

from .email_notify import send_email

KEYRING_SERVICE = "compression_service"
_INDEX_USER = "__backup_handler_index__"


# ─── Keyring helpers ────────────────────────────────────────────────────────


def _load_index():
    raw = keyring.get_password(KEYRING_SERVICE, _INDEX_USER)
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []


def save_file_passwd(logger, timestamp, passwd):
    """Store an archive password in the keyring and record it in the index."""
    try:
        keyring.set_password(KEYRING_SERVICE, timestamp, passwd)
        index = _load_index()
        if timestamp not in index:
            index.append(timestamp)
            keyring.set_password(KEYRING_SERVICE, _INDEX_USER, json.dumps(index))
        logger.info(f"Password stored securely for '{timestamp}'")
    except Exception as e:
        logger.error(f"Failed to store password securely: {e}")


def get_password_by_timestamp(timestamp, logger):
    """Return the archive password stored for ``timestamp``, or None."""
    try:
        password = keyring.get_password(KEYRING_SERVICE, timestamp)
    except keyring.errors.KeyringError as e:
        logger.error(f"Failed to retrieve password for timestamp '{timestamp}': {e}")
        return None
    if password:
        logger.info(f"Password retrieved successfully for timestamp: {timestamp}")
    else:
        logger.warning(f"No password found for timestamp: {timestamp}")
    return password


def clear_keyring(logger=None):
    """Delete every archive password this tool stored, plus the index itself."""
    removed = 0
    try:
        for timestamp in _load_index():
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(KEYRING_SERVICE, timestamp)
                removed += 1
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(KEYRING_SERVICE, _INDEX_USER)
        msg = f"Cleared {removed} stored archive password(s) from service '{KEYRING_SERVICE}'."
    except keyring.errors.KeyringError as e:
        msg = f"Failed to clear keyring for service '{KEYRING_SERVICE}': {e}"
    if logger:
        logger.info(msg)
    else:
        print(msg)
    return removed


# ─── Archiving ──────────────────────────────────────────────────────────────


def _write_archive(src_dir, output_zip, password=None):
    """Write ``src_dir`` into ``output_zip`` (AES-256 if ``password``), atomically."""
    src_dir = Path(src_dir)
    output_zip = Path(output_zip)
    fd, tmp = tempfile.mkstemp(prefix=f".{output_zip.name}.", suffix=".tmp", dir=output_zip.parent)
    os.close(fd)
    tmp = Path(tmp)
    try:
        kwargs = {"compression": pyzipper.ZIP_DEFLATED}
        if password:
            kwargs["encryption"] = pyzipper.WZ_AES
        with pyzipper.AESZipFile(tmp, "w", **kwargs) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
                zf.setencryption(pyzipper.WZ_AES, nbits=256)
            for root, _dirs, file_list in os.walk(src_dir):
                for name in sorted(file_list):
                    path = Path(root) / name
                    zf.write(path, arcname=path.relative_to(src_dir).as_posix())
        os.replace(tmp, output_zip)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def extract_archive(zip_path, to_dir, password=None):
    """Extract a (possibly AES-encrypted) ZIP. Member paths cannot escape ``to_dir``."""
    with pyzipper.AESZipFile(zip_path, "r") as zf:
        if password:
            zf.setpassword(password.encode("utf-8"))
        zf.extractall(to_dir)


def compress_directory(
    logger,
    src_dirs=None,
    output_dirs=None,
    password=None,
    bot_handler=None,
    receiver_emails=None,
    send_password=True,
):
    """
    Compress multiple source directories into ZIP files with optional password protection.
    The output ZIP files will be saved in the corresponding output directories.

    Parameters:
    - logger (logging.Logger): The logger instance to use for logging messages.
    - src_dirs (list of str): The list of paths to the source directories to be compressed.
    - output_dirs (list of str): The list of output directories where the resulting ZIP files will be saved.
    - password (str, optional): If provided, the ZIP files are encrypted with AES-256.
    - bot_handler (TelegramBot, optional): Deliver the password to authorised bot users.
    - receiver_emails (list of str, optional): Deliver the password via email.
    - send_password (bool): When False the password is kept in the keyring only.

    Returns:
    - bool: True if every archive was written.
    """
    if src_dirs is None or output_dirs is None:
        logger.error("Source and output directories must be provided.")
        return False

    ok = True
    for src_dir in src_dirs:
        for output_dir in output_dirs:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_zip = os.path.join(output_dir, f"backup_{timestamp}.zip")

            try:
                if password:
                    _write_archive(src_dir, output_zip, password)
                    logger.info(f"Compressed directory '{src_dir}' to '{output_zip}' with AES-256 password")
                    save_file_passwd(logger, timestamp, password)
                else:
                    shutil.make_archive(output_zip[:-4], "zip", src_dir)
                    logger.info(
                        f"Compressed directory '{src_dir}' to '{output_zip}' without password protection"
                    )

                if password and send_password:
                    if bot_handler:
                        _send_password_via_bot(logger, bot_handler, timestamp, password)
                    if receiver_emails:
                        _send_password_via_email(logger, receiver_emails, timestamp, password)

            except Exception as e:
                ok = False
                logger.error(f"Failed to compress directory '{src_dir}' to '{output_zip}': {e}")
    return ok


def _send_password_via_bot(logger, bot_handler, timestamp, password):
    """Send the password to authorised Telegram users as an in-memory document."""
    try:
        content = f"{timestamp}: {password}\n"
        buf = io.BytesIO(content.encode("utf-8"))
        buf.name = "backup_password.txt"

        bot_handler.send_document(document=buf, caption="Here is your backup password.")
        logger.info("Sent backup password to users via bot.")
    except Exception as e:
        logger.error(f"Failed to send password via bot: {e}")


def _send_password_via_email(logger, receiver_emails, timestamp, password):
    """Send the password by email as an attachment (temp file is owner-only and removed)."""
    try:
        content = f"{timestamp}: {password}\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            subject = "Backup Password"
            body = "Please find the attached file containing your backup password."
            send_email(receiver_emails, subject, body, attachment_paths=[tmp_path], logger=logger)
            logger.info("Sent backup password to users via email.")
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Failed to send password via email: {e}")
