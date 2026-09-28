"""Tests for AES-256 archives and keyring bookkeeping (keyring mocked)."""

from __future__ import annotations

import zipfile
from unittest import mock

import pytest

from src import compression


@pytest.fixture
def fake_keyring(monkeypatch):
    store = {}

    def delete(service, user):
        if (service, user) not in store:
            raise compression.keyring.errors.PasswordDeleteError(user)
        del store[(service, user)]

    monkeypatch.setattr(compression.keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(compression.keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(compression.keyring, "delete_password", delete)
    return store


def test_password_archive_is_aes_and_keeps_paths(tmp_dir, logger, fake_keyring):
    src, out = tmp_dir / "src", tmp_dir / "out"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A")
    (src / "sub" / "a.txt").write_text("B")
    out.mkdir()
    assert compression.compress_directory(logger, [str(src)], [str(out)], password="pw")
    archive = next(out.glob("backup_*.zip"))
    with zipfile.ZipFile(archive) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/a.txt"]
        assert all(info.flag_bits & 0x1 for info in zf.infolist())
        assert all(info.compress_type == 99 for info in zf.infolist())  # 99 = WinZip AES
    with pytest.raises(RuntimeError):
        compression.extract_archive(archive, tmp_dir / "x")
    compression.extract_archive(archive, tmp_dir / "x", "pw")
    assert (tmp_dir / "x" / "sub" / "a.txt").read_text() == "B"


def test_password_not_sent_when_disabled(tmp_dir, logger, fake_keyring):
    (tmp_dir / "src").mkdir()
    (tmp_dir / "src" / "f").write_text("x")
    bot = mock.Mock()
    compression.compress_directory(
        logger, [str(tmp_dir / "src")], [str(tmp_dir)], password="pw", bot_handler=bot, send_password=False
    )
    bot.send_document.assert_not_called()
    assert "pw" in fake_keyring.values()


def test_clear_keyring_removes_every_stored_password(logger, fake_keyring):
    compression.save_file_passwd(logger, "20260101_000000", "a")
    compression.save_file_passwd(logger, "20260102_000000", "b")
    assert compression.clear_keyring(logger) == 2
    assert fake_keyring == {}


def test_restore_uses_keyring_password_for_aes_zip(tmp_dir, logger, fake_keyring):
    from src.restore import restore_backup

    (tmp_dir / "src").mkdir()
    (tmp_dir / "src" / "f.txt").write_text("data")
    compression._write_archive(tmp_dir / "src", tmp_dir / "backup_20260101_120000.zip", "pw")
    compression.save_file_passwd(logger, "20260101_120000", "pw")
    assert restore_backup(logger, str(tmp_dir / "backup_20260101_120000.zip"), str(tmp_dir / "out"))
    assert (tmp_dir / "out" / "f.txt").read_text() == "data"
