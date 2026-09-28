"""Tests for local sync and SFTP upload (against an in-memory fake SFTP server)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import sync
from src.encryption import EncryptionKey, decrypt_stream
from src.manifest import BackupManifest
from src.utils import calculate_checksum, copy_and_verify


class FakeSFTP:
    """Minimal SFTP client that stores 'remote' files under a local root."""

    def __init__(self, root: Path):
        self.root = root

    def _p(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    def stat(self, remote):
        p = self._p(remote)
        if not p.exists():
            raise FileNotFoundError(remote)
        return p.stat()

    def mkdir(self, remote):
        self._p(remote).mkdir()

    def put(self, local, remote):
        shutil.copyfile(local, self._p(remote))

    def open(self, remote, mode):
        f = open(self._p(remote), mode)  # noqa: SIM115
        f.set_pipelined = lambda _flag: None
        return f

    def listdir_attr(self, remote):
        p = self._p(remote)
        return [SimpleNamespace(filename=c.name, st_mode=c.stat().st_mode) for c in p.iterdir()]

    def remove(self, remote):
        self._p(remote).unlink()


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)


class TestCopyAndVerify:
    def test_returns_source_checksum_and_preserves_mtime(self, tmp_dir):
        src = tmp_dir / "src.bin"
        src.write_bytes(os.urandom(3 * 1024 * 1024 + 7))
        os.utime(src, (1_000_000, 1_000_000))
        checksum = copy_and_verify(src, tmp_dir / "dst.bin")
        assert checksum == calculate_checksum(src)
        assert (tmp_dir / "dst.bin").stat().st_mtime == 1_000_000

    def test_returns_none_on_mismatch(self, tmp_dir):
        (tmp_dir / "a").write_text("a")
        with mock.patch("src.utils.calculate_checksum", return_value="different"):
            assert copy_and_verify(tmp_dir / "a", tmp_dir / "b") is None


class TestLocalSync:
    def test_full_backup_copies_tree_and_records_checksums(self, tmp_dir, logger):
        src, dst = tmp_dir / "src", tmp_dir / "dst"
        _tree(src, {"a.txt": "A", "sub/b.txt": "B", "skip.log": "x"})
        manifest = BackupManifest()
        sync.perform_full_backup(logger, str(src), [str(dst)], exclude_patterns=["*.log"], manifest=manifest)
        assert (dst / "a.txt").read_text() == "A"
        assert (dst / "sub" / "b.txt").read_text() == "B"
        assert not (dst / "skip.log").exists()
        copied = {Path(e["path"]).name: e["checksum"] for e in manifest._copied}
        assert copied["a.txt"] == calculate_checksum(src / "a.txt")

    def test_incremental_skips_unchanged(self, tmp_dir, logger):
        src, dst = tmp_dir / "src", tmp_dir / "dst"
        _tree(src, {"old.txt": "o", "new.txt": "n"})
        _tree(dst, {"old.txt": "o"})
        os.utime(src / "old.txt", (100, 100))
        manifest = BackupManifest()
        sync.perform_incremental_backup(logger, str(src), [str(dst)], 1000, manifest=manifest)
        assert [Path(e["path"]).name for e in manifest._copied] == ["new.txt"]
        assert [Path(e["path"]).name for e in manifest._skipped] == ["old.txt"]


class TestSftpUpload:
    def test_plain_upload_and_full_mode_cleanup(self, tmp_dir, logger):
        src, remote = tmp_dir / "src", tmp_dir / "remote"
        _tree(src, {"a.txt": "A", "sub/b.txt": "B"})
        remote.mkdir()
        _tree(remote / str(src).lstrip("/"), {"stale.txt": "old"})
        failed = sync._sftp_upload_directory(FakeSFTP(remote), str(src), str(src), mode="full", logger=logger)
        base = remote / str(src).lstrip("/")
        assert failed == 0
        assert (base / "sub" / "b.txt").read_text() == "B"
        assert not (base / "stale.txt").exists()

    def test_encrypted_upload_never_sends_plaintext(self, tmp_dir, logger):
        src, remote = tmp_dir / "src", tmp_dir / "remote"
        _tree(src, {"secret.txt": "top secret", "sub/b.txt": "B"})
        remote.mkdir()
        key = EncryptionKey.create(passphrase="pw")
        sync._sftp_upload_directory(
            FakeSFTP(remote), str(src), str(src), mode="full", logger=logger, encryption_key=key
        )
        base = remote / str(src).lstrip("/")
        names = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
        assert names == ["secret.txt.enc", "sub/b.txt.enc"]
        assert b"top secret" not in (base / "secret.txt.enc").read_bytes()
        decrypt_stream(base / "secret.txt.enc", tmp_dir / "out", passphrase="pw")
        assert (tmp_dir / "out").read_text() == "top secret"

    def test_upload_failures_are_counted(self, tmp_dir, logger):
        src, remote = tmp_dir / "src", tmp_dir / "remote"
        _tree(src, {"a.txt": "A"})
        remote.mkdir()
        with mock.patch.object(sync, "_sftp_put_throttled", side_effect=OSError("boom")):
            failed = sync._sftp_upload_directory(FakeSFTP(remote), str(src), str(src), logger=logger)
        assert failed == 1

    def test_concurrent_sync_reports_failed_servers(self, logger):
        def fake(source_dir, server, *args, **kwargs):
            if server == "bad":
                raise RuntimeError("unreachable")

        with mock.patch.object(sync, "sync_ssh_server", side_effect=fake):
            failed = sync.sync_ssh_servers_concurrently("/src", ["good", "bad"], "user", logger=logger)
        assert failed == ["bad"]


class TestHostKeyPolicy:
    def test_strict_mode_rejects_unknown_hosts(self):
        import paramiko

        client = sync.make_ssh_client(strict_host_key_checking=True)
        assert isinstance(client._policy, paramiko.RejectPolicy)

    def test_default_warns_but_loads_known_hosts(self, tmp_dir):
        import paramiko

        kh = tmp_dir / "known_hosts"
        key = paramiko.RSAKey.generate(1024)
        kh.write_text(f"backup.example {key.get_name()} {key.get_base64()}\n")
        client = sync.make_ssh_client(known_hosts_file=str(kh))
        assert isinstance(client._policy, paramiko.WarningPolicy)
        assert "backup.example" in client.get_host_keys()
