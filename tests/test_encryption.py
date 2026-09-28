"""Tests for AES-256-GCM encryption at rest."""

from __future__ import annotations

import os

import pytest

from src.encryption import (
    decrypt_directory,
    decrypt_file,
    derive_key,
    encrypt_directory,
    encrypt_file,
    load_key_file,
)


class TestEncryption:
    def test_encrypt_decrypt_file_passphrase(self, tmp_dir):
        test_file = tmp_dir / "test.txt"
        test_file.write_text("hello encryption")
        passphrase = "test_passphrase_123"

        enc_path = encrypt_file(test_file, passphrase=passphrase)
        assert enc_path.suffix == ".enc"
        assert enc_path.exists()
        assert not test_file.exists()

        dec_path = decrypt_file(enc_path, passphrase=passphrase)
        assert dec_path.read_text() == "hello encryption"
        assert not enc_path.exists()

    def test_encrypt_decrypt_file_keyfile(self, tmp_dir):
        test_file = tmp_dir / "test.txt"
        test_file.write_bytes(b"keyfile test data")
        key_file = tmp_dir / "keyfile.bin"
        key_file.write_bytes(os.urandom(32))

        enc_path = encrypt_file(test_file, key_file=str(key_file))
        assert enc_path.exists()

        dec_path = decrypt_file(enc_path, key_file=str(key_file))
        assert dec_path.read_bytes() == b"keyfile test data"

    def test_encrypt_directory_skips_manifests(self, tmp_dir, logger):
        (tmp_dir / "data.txt").write_text("data")
        (tmp_dir / "backup_manifest_20260101_120000.json").write_text("{}")

        count = encrypt_directory(tmp_dir, passphrase="pass", logger=logger)
        assert count == 1
        assert (tmp_dir / "data.txt.enc").exists()
        assert (tmp_dir / "backup_manifest_20260101_120000.json").exists()

    def test_encrypt_directory_skips_enc_files(self, tmp_dir, logger):
        (tmp_dir / "already.enc").write_bytes(b"encrypted")
        (tmp_dir / "new.txt").write_text("new")

        count = encrypt_directory(tmp_dir, passphrase="pass", logger=logger)
        assert count == 1

    def test_decrypt_directory(self, tmp_dir, logger):
        (tmp_dir / "a.txt").write_text("aaa")
        (tmp_dir / "b.txt").write_text("bbb")
        encrypt_directory(tmp_dir, passphrase="pass", logger=logger)

        assert (tmp_dir / "a.txt.enc").exists()
        assert (tmp_dir / "b.txt.enc").exists()

        decrypt_directory(tmp_dir, passphrase="pass", logger=logger)
        assert (tmp_dir / "a.txt").read_text() == "aaa"
        assert (tmp_dir / "b.txt").read_text() == "bbb"

    def test_derive_key_deterministic(self):
        salt = b"\x00" * 16
        key1 = derive_key("passphrase", salt)
        key2 = derive_key("passphrase", salt)
        assert key1 == key2
        assert len(key1) == 32

    def test_load_key_file_wrong_size(self, tmp_dir):
        kf = tmp_dir / "bad.key"
        kf.write_bytes(b"\x00" * 16)
        with pytest.raises(ValueError, match="32 bytes"):
            load_key_file(str(kf))

    def test_wrong_passphrase_fails(self, tmp_dir):
        test_file = tmp_dir / "test.txt"
        test_file.write_text("secret data")
        enc_path = encrypt_file(test_file, passphrase="correct")

        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            decrypt_file(enc_path, passphrase="wrong")


class TestStreamingFormat:
    """v2 chunked format, legacy compatibility and bulk behaviour."""

    def test_multi_chunk_roundtrip_at_boundaries(self, tmp_dir):
        from src.encryption import EncryptionKey, decrypt_stream, encrypt_stream

        key = EncryptionKey.create(passphrase="pw")
        for size in (0, 1, 15, 16, 17, 32, 100):
            src = tmp_dir / f"f{size}"
            data = os.urandom(size)
            src.write_bytes(data)
            encrypt_stream(src, tmp_dir / "x.enc", key, chunk_size=16)
            decrypt_stream(tmp_dir / "x.enc", tmp_dir / "out", passphrase="pw")
            assert (tmp_dir / "out").read_bytes() == data

    def test_legacy_v1_files_still_decrypt(self, tmp_dir):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt, nonce = os.urandom(16), os.urandom(12)
        key = derive_key("pw", salt)
        (tmp_dir / "old.txt.enc").write_bytes(salt + nonce + AESGCM(key).encrypt(nonce, b"legacy", None))
        assert decrypt_file(tmp_dir / "old.txt.enc", passphrase="pw").read_bytes() == b"legacy"

    def test_truncation_at_chunk_boundary_is_detected(self, tmp_dir):
        from cryptography.exceptions import InvalidTag

        from src.encryption import _HEADER_SIZE, TAG_SIZE, EncryptionKey, decrypt_stream, encrypt_stream

        (tmp_dir / "f").write_bytes(os.urandom(48))
        encrypt_stream(tmp_dir / "f", tmp_dir / "f.enc", EncryptionKey.create(passphrase="pw"), chunk_size=16)
        raw = (tmp_dir / "f.enc").read_bytes()
        (tmp_dir / "t.enc").write_bytes(raw[: _HEADER_SIZE + 2 * (16 + TAG_SIZE)])
        with pytest.raises(InvalidTag):
            decrypt_stream(tmp_dir / "t.enc", tmp_dir / "t", passphrase="pw")
        assert not (tmp_dir / "t").exists()  # atomic: no partial plaintext left behind

    def test_header_tampering_is_detected(self, tmp_dir):
        from cryptography.exceptions import InvalidTag

        (tmp_dir / "f").write_bytes(b"data")
        enc = encrypt_file(tmp_dir / "f", key_file=None, passphrase="pw")
        raw = bytearray(enc.read_bytes())
        raw[-5] ^= 0x01
        enc.write_bytes(bytes(raw))
        with pytest.raises(InvalidTag):
            decrypt_file(enc, passphrase="pw")

    def test_passphrase_kdf_runs_once_per_directory_run(self, tmp_dir, logger):
        from unittest import mock

        import src.encryption as enc

        for i in range(5):
            (tmp_dir / f"f{i}").write_text(str(i))
        enc._derive_key_cached.cache_clear()
        with mock.patch.object(enc, "PBKDF2HMAC", wraps=enc.PBKDF2HMAC) as kdf:
            assert encrypt_directory(tmp_dir, passphrase="unique-pw-1", logger=logger) == 5
            decrypt_directory(tmp_dir, passphrase="unique-pw-1", logger=logger)
        assert kdf.call_count == 1

    def test_hardlinks_survive_encryption(self, tmp_dir, logger):
        from src.encryption import encrypt_directories

        a, b = tmp_dir / "d1" / "a", tmp_dir / "d2" / "b"
        a.parent.mkdir()
        b.parent.mkdir()
        a.write_text("same")
        os.link(a, b)
        result = encrypt_directories([a.parent, b.parent], passphrase="pw", logger=logger)
        assert (result.succeeded, result.failed) == (2, 0)
        assert (tmp_dir / "d1" / "a.enc").stat().st_ino == (tmp_dir / "d2" / "b.enc").stat().st_ino

    def test_failures_are_counted(self, tmp_dir, logger):
        from unittest import mock

        import src.encryption as enc

        (tmp_dir / "f").write_text("x")
        with mock.patch.object(enc, "encrypt_stream", side_effect=OSError("disk full")):
            result = enc.encrypt_directories([tmp_dir], passphrase="pw", logger=logger)
        assert (result.succeeded, result.failed) == (0, 1)
        assert (tmp_dir / "f").exists()

    def test_upload_source_encrypts_to_temp_copy(self, tmp_dir):
        from src.encryption import EncryptionKey, decrypt_stream, upload_source

        (tmp_dir / "f").write_bytes(b"plain")
        with upload_source(tmp_dir / "f", EncryptionKey.create(passphrase="pw")) as src:
            assert src != tmp_dir / "f" and b"plain" not in src.read_bytes()
            decrypt_stream(src, tmp_dir / "check", passphrase="pw")
            temp_path = src
        assert not temp_path.exists()
        assert (tmp_dir / "check").read_bytes() == b"plain"
        with upload_source(tmp_dir / "f", None) as src:
            assert src == tmp_dir / "f"
