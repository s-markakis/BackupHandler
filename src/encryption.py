"""
encryption.py - AES-256-GCM Encryption at Rest

Provides file-level encryption and decryption for backup data using
AES-256-GCM authenticated encryption. Supports two key sources:

  1. Key file   - A raw 32-byte key read directly from disk.
  2. Passphrase - Derived via PBKDF2-HMAC-SHA256 with 600,000 iterations.

Current file format (v2, streaming):

    header = MAGIC (8) | key source (1) | salt (16) | file seed (16) | chunk size (4, big-endian)
    body   = chunk_0 | chunk_1 | ... | chunk_n       (each: ciphertext + 16-byte GCM tag)

  * A per-file key is derived with HKDF-SHA256(master_key, salt=file seed),
    so nonces are a simple chunk counter and can never repeat under one key.
  * Every chunk is authenticated together with the header and a "final
    chunk" flag, so reordering, truncation and header tampering are detected.
  * Files are processed chunk by chunk — memory use is independent of size.
  * The passphrase KDF runs once per salt (one salt per directory run),
    not once per file.

Legacy format (v1), still readable:
    [16 bytes salt][12 bytes nonce][ciphertext + 16 bytes GCM auth tag]
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from tqdm import tqdm

# ─── Cryptographic constants ────────────────────────────────────────────────
PBKDF2_ITERATIONS = 600_000  # OWASP-recommended minimum for HMAC-SHA256
SALT_SIZE = 16  # 128-bit random salt
NONCE_SIZE = 12  # 96-bit nonce (standard for AES-GCM)
KEY_SIZE = 32  # 256-bit AES key
TAG_SIZE = 16  # GCM authentication tag

MAGIC = b"BHENC\x00\x02\x00"
SEED_SIZE = 16
DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB of plaintext per authenticated chunk
_HEADER_SIZE = len(MAGIC) + 1 + SALT_SIZE + SEED_SIZE + 4
_SOURCE_KEYFILE = 0
_SOURCE_PASSPHRASE = 1
_HKDF_INFO = b"backup-handler file key v2"


# ─── Key handling ───────────────────────────────────────────────────────────


@lru_cache(maxsize=32)
def _derive_key_cached(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def derive_key(passphrase, salt):
    """Derive a 32-byte AES key from a passphrase using PBKDF2-HMAC-SHA256.

    Results are cached per (passphrase, salt) so decrypting a directory
    encrypted in one run costs a single KDF invocation.
    """
    if passphrase is None:
        raise ValueError("A passphrase is required to decrypt this file")
    return _derive_key_cached(passphrase, bytes(salt))


def load_key_file(path):
    """Read a raw 32-byte key from a file."""
    key_path = Path(path)
    if not key_path.exists():
        raise FileNotFoundError(f"Key file not found: {path}")
    key = key_path.read_bytes()
    if len(key) != KEY_SIZE:
        raise ValueError(f"Key file must be exactly {KEY_SIZE} bytes, got {len(key)}")
    return key


def get_encryption_key(passphrase=None, key_file=None, salt=None):
    """
    Get encryption key from either a key file or passphrase.
    Key file takes priority if both are provided.
    Returns (key_bytes, salt) — salt is None when using key_file.
    """
    if key_file:
        return load_key_file(key_file), None
    if passphrase:
        if salt is None:
            salt = os.urandom(SALT_SIZE)
        return derive_key(passphrase, salt), salt
    raise ValueError("Either passphrase or key_file must be provided for encryption")


@dataclass(frozen=True)
class EncryptionKey:
    """A master key ready for encrypting many files (derive once, reuse)."""

    key: bytes
    salt: bytes
    source: int

    @classmethod
    def create(cls, passphrase=None, key_file=None):
        key, salt = get_encryption_key(passphrase=passphrase, key_file=key_file)
        if salt is None:
            return cls(key=key, salt=b"\x00" * SALT_SIZE, source=_SOURCE_KEYFILE)
        return cls(key=key, salt=salt, source=_SOURCE_PASSPHRASE)


def _file_key(master_key: bytes, seed: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=seed, info=_HKDF_INFO).derive(master_key)


def _chunk_nonce(index: int) -> bytes:
    return index.to_bytes(NONCE_SIZE, "big")


# ─── Stream primitives ──────────────────────────────────────────────────────


def _atomic_output(target: Path):
    """Create a temp file next to ``target``; returns (fd, tmp_path)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    return fd, Path(tmp)


def encrypt_stream(src, dst, key: EncryptionKey, chunk_size=DEFAULT_CHUNK_SIZE):
    """Encrypt the file at ``src`` into ``dst`` (v2 format), atomically.

    ``dst`` is only replaced once the whole file has been written, so a crash
    never leaves a truncated ciphertext behind. Metadata (mode, mtime) is
    copied from ``src``.
    """
    src, dst = Path(src), Path(dst)
    seed = os.urandom(SEED_SIZE)
    header = MAGIC + bytes([key.source]) + key.salt + seed + chunk_size.to_bytes(4, "big")
    aesgcm = AESGCM(_file_key(key.key, seed))

    fd, tmp = _atomic_output(dst)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            out.write(header)
            index = 0
            chunk = inp.read(chunk_size)
            while True:
                nxt = inp.read(chunk_size) if len(chunk) == chunk_size else b""
                final = not nxt
                out.write(aesgcm.encrypt(_chunk_nonce(index), chunk, header + bytes([final])))
                if final:
                    break
                chunk = nxt
                index += 1
        shutil.copystat(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst


def _decrypt_v2(inp, out, header, passphrase, key_file):
    source = header[len(MAGIC)]
    salt = header[len(MAGIC) + 1 : len(MAGIC) + 1 + SALT_SIZE]
    seed = header[len(MAGIC) + 1 + SALT_SIZE : len(MAGIC) + 1 + SALT_SIZE + SEED_SIZE]
    chunk_size = int.from_bytes(header[-4:], "big")
    if chunk_size <= 0 or chunk_size > 64 * 1024 * 1024:
        raise ValueError("Corrupt encrypted file header (chunk size)")

    if source == _SOURCE_KEYFILE:
        if not key_file:
            raise ValueError("This file was encrypted with a key file; key_file is required")
        master = load_key_file(key_file)
    elif source == _SOURCE_PASSPHRASE:
        master = derive_key(passphrase, salt)
    else:
        raise ValueError("Corrupt encrypted file header (key source)")

    aesgcm = AESGCM(_file_key(master, seed))
    block = chunk_size + TAG_SIZE
    index = 0
    current = inp.read(block)
    while True:
        nxt = inp.read(block) if len(current) == block else b""
        final = not nxt
        # Raises InvalidTag on wrong key, tampering, reordering or truncation.
        out.write(aesgcm.decrypt(_chunk_nonce(index), current, header + bytes([final])))
        if final:
            return
        current = nxt
        index += 1


def decrypt_stream(src, dst, passphrase=None, key_file=None):
    """Decrypt ``src`` (v1 or v2 format) into ``dst`` atomically."""
    src, dst = Path(src), Path(dst)
    fd, tmp = _atomic_output(dst)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            header = inp.read(_HEADER_SIZE)
            if header.startswith(MAGIC) and len(header) == _HEADER_SIZE:
                _decrypt_v2(inp, out, header, passphrase, key_file)
            else:
                # Legacy v1: whole-file AES-GCM
                data = header + inp.read()
                salt = data[:SALT_SIZE]
                nonce = data[SALT_SIZE : SALT_SIZE + NONCE_SIZE]
                key = load_key_file(key_file) if key_file else derive_key(passphrase, salt)
                out.write(AESGCM(key).decrypt(nonce, data[SALT_SIZE + NONCE_SIZE :], None))
        shutil.copystat(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst


# ─── File-level API ─────────────────────────────────────────────────────────


def _enc_name(path: Path) -> Path:
    return path.with_name(path.name + ".enc")


def _dec_name(enc_path: Path) -> Path:
    if enc_path.name.endswith(".enc"):
        return enc_path.with_name(enc_path.name[:-4])
    return enc_path.with_suffix("")


def encrypt_file(path, passphrase=None, key_file=None, key: EncryptionKey | None = None):
    """
    Encrypt a single file with AES-256-GCM.

    Writes an encrypted copy at ``<original>.enc`` and deletes the plaintext
    original. Pass a prepared ``key`` to avoid re-running the passphrase KDF.

    Returns:
        Path: Path to the newly created ``.enc`` file.
    """
    path = Path(path)
    if key is None:
        key = EncryptionKey.create(passphrase=passphrase, key_file=key_file)
    enc_path = encrypt_stream(path, _enc_name(path), key)
    path.unlink()
    return enc_path


def decrypt_file(enc_path, passphrase=None, key_file=None):
    """
    Decrypt a ``.enc`` file back to its original plaintext name and delete
    the encrypted copy.

    Raises:
        cryptography.exceptions.InvalidTag: If the file is corrupted or the wrong key is used.
    """
    enc_path = Path(enc_path)
    out_path = decrypt_stream(enc_path, _dec_name(enc_path), passphrase=passphrase, key_file=key_file)
    enc_path.unlink()
    return out_path


@contextlib.contextmanager
def upload_source(path, key: EncryptionKey | None):
    """Yield the file to upload for ``path``: itself, or an encrypted temp copy.

    Used by remote targets (SFTP, S3) so plaintext never leaves the host
    when encryption is enabled. The temp copy lives in a private directory
    and is removed afterwards.
    """
    if key is None:
        yield Path(path)
        return
    with tempfile.TemporaryDirectory(prefix="backup-handler-enc-") as tmp:
        dst = Path(tmp) / (Path(path).name + ".enc")
        encrypt_stream(path, dst, key)
        yield dst


def remote_name(relative: str, key: EncryptionKey | None) -> str:
    """Name a file gets on a remote target (``.enc`` appended when encrypting)."""
    return f"{relative}.enc" if key is not None else str(relative)


# ─── Directory-level API ────────────────────────────────────────────────────


@dataclass
class DirectoryResult:
    """Outcome of a bulk encrypt/decrypt pass."""

    succeeded: int = 0
    failed: int = 0


def _is_manifest(f: Path) -> bool:
    return f.name.startswith("backup_manifest_") and f.suffix == ".json"


def _run_parallel(items, fn, workers, desc, logger, verb):
    result = DirectoryResult()
    lock = threading.Lock()

    def _one(item):
        try:
            count = fn(item) or 1
            with lock:
                result.succeeded += count
            if logger:
                logger.debug(f"{verb}: {item}")
        except Exception as e:
            with lock:
                result.failed += len(item) if isinstance(item, list) else 1
            if logger:
                logger.error(f"Failed to {verb.lower()} {item}: {e}")

    workers = max(1, workers)
    if workers == 1:
        for item in tqdm(items, desc=desc, unit="files"):
            _one(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_one, i) for i in items]
            for _ in tqdm(as_completed(futures), total=len(futures), desc=desc, unit="files"):
                pass
    return result


def encrypt_directories(directories, passphrase=None, key_file=None, logger=None, workers=1):
    """
    Encrypt all eligible files in one or more directory trees.

    * The passphrase KDF runs once for the whole call.
    * Hardlinked files (e.g. produced by dedup) are encrypted once and the
      resulting ``.enc`` files are hardlinked the same way, so dedup savings
      survive encryption — including across directories.
    * Manifests and existing ``.enc`` files are skipped.

    Returns:
        DirectoryResult: counts of encrypted and failed files.
    """
    key = EncryptionKey.create(passphrase=passphrase, key_file=key_file)

    # Group paths by inode so each distinct content is encrypted once.
    groups: dict[tuple[int, int], list[Path]] = {}
    for directory in directories:
        for f in sorted(Path(directory).rglob("*")):
            if not f.is_file() or f.is_symlink() or f.suffix == ".enc" or _is_manifest(f):
                continue
            st = f.stat()
            groups.setdefault((st.st_dev, st.st_ino), []).append(f)

    def _encrypt_group(paths):
        first = paths[0]
        first_enc = encrypt_stream(first, _enc_name(first), key)
        for other in paths[1:]:
            other_enc = _enc_name(other)
            tmp = other_enc.with_name(f".{other_enc.name}.link")
            tmp.unlink(missing_ok=True)
            os.link(first_enc, tmp)
            os.replace(tmp, other_enc)
        for p in paths:
            p.unlink()
        return len(paths)

    return _run_parallel(
        list(groups.values()), _encrypt_group, workers, "Encrypting files", logger, "Encrypted"
    )


def encrypt_directory(directory, passphrase=None, key_file=None, logger=None, workers=1):
    """Encrypt one directory tree. Returns the number of files encrypted."""
    result = encrypt_directories([directory], passphrase, key_file, logger, workers)
    if logger:
        logger.info(f"Encrypted {result.succeeded} files in {directory}")
    return result.succeeded


def decrypt_directory(directory, passphrase=None, key_file=None, logger=None, workers=1):
    """
    Decrypt all ``.enc`` files in a directory tree.

    Returns:
        int: Number of files successfully decrypted.
    """
    directory = Path(directory)
    files = [f for f in directory.rglob("*.enc") if f.is_file()]
    if not files:
        return 0

    def _decrypt_one(f):
        decrypt_file(f, passphrase=passphrase, key_file=key_file)

    result = _run_parallel(files, _decrypt_one, workers, "Decrypting files", logger, "Decrypted")
    if logger:
        logger.info(f"Decrypted {result.succeeded} files in {directory}")
    return result.succeeded
