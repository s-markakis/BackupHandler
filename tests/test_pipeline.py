"""End-to-end test of one local backup run through backup_operation."""

from __future__ import annotations

from unittest import mock

import pytest

from src import utils
from src.cli import pipeline
from src.encryption import decrypt_directory


@pytest.fixture
def run_env(tmp_dir, monkeypatch):
    monkeypatch.setattr(utils, "TIMESTAMP_FILE", tmp_dir / "ts" / "last.json")
    monkeypatch.setattr(utils, "FULL_BACKUP_TIMESTAMP_FILE", tmp_dir / "ts" / "full.json")
    src = tmp_dir / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("same content")
    (src / "sub" / "copy.txt").write_text("same content")
    (src / "b.txt").write_text("other")
    config = {
        "preflight_enabled": False,
        "preflight_status_sentinel": str(tmp_dir / "status.json"),
        "encryption_enabled": True,
        "encryption_passphrase": "pw",
        "dedup_enabled": True,
    }
    return tmp_dir, src, config


def _run(logger, src, backup, config):
    return pipeline.backup_operation(
        logger,
        source_dir=str(src),
        backup_dirs=[str(backup)],
        operation_modes=["local"],
        backup_mode="full",
        config_values=config,
    )


def test_local_backup_dedups_then_encrypts(run_env, logger):
    tmp_dir, src, config = run_env
    backup = tmp_dir / "backup"
    assert _run(logger, src, backup, config) == 0

    enc = {p.relative_to(backup).as_posix() for p in backup.rglob("*.enc")}
    assert enc == {"a.txt.enc", "sub/copy.txt.enc", "b.txt.enc"}
    assert not list(backup.rglob("*.txt"))  # no plaintext left
    # dedup ran before encryption, and encryption kept the hardlink
    assert (backup / "a.txt.enc").stat().st_ino == (backup / "sub" / "copy.txt.enc").stat().st_ino
    assert list(backup.glob("backup_manifest_*.json"))

    decrypt_directory(backup, passphrase="pw", logger=logger)
    assert (backup / "sub" / "copy.txt").read_text() == "same content"


def test_encryption_failure_fails_the_run(run_env, logger):
    tmp_dir, src, config = run_env
    with mock.patch("src.encryption.encrypt_stream", side_effect=OSError("disk full")):
        rc = _run(logger, src, tmp_dir / "backup", config)
    assert rc == 3
    assert not utils.TIMESTAMP_FILE.exists()  # a failed run must not advance the marker


def test_dry_run_touches_nothing(run_env, logger):
    tmp_dir, src, config = run_env
    rc = pipeline.backup_operation(
        logger,
        source_dir=str(src),
        backup_dirs=[str(tmp_dir / "backup")],
        operation_modes=["local", "s3", "ssh"],
        backup_mode="full",
        dry_run=True,
        ssh_servers=["host"],
        config_values={**config, "s3_bucket": "b"},
    )
    assert rc == 0
    assert not (tmp_dir / "backup").exists()


def test_ssh_mode_uses_config_credentials_when_cli_omits_them(run_env, logger):
    _tmp_dir, src, config = run_env
    config = {
        **config,
        "encryption_enabled": False,
        "ssh_servers": ["backup.example"],
        "ssh_username": "backup",
        "ssh_password": "from-config",
        "ssh_key_file": "/keys/id_ed25519",
    }
    with mock.patch.object(pipeline, "sync_ssh_servers_concurrently", return_value=[]) as ssh:
        rc = pipeline.backup_operation(
            logger, source_dir=str(src), backup_dirs=[], operation_modes=["ssh"], config_values=config
        )
    assert rc == 0
    args, kwargs = ssh.call_args
    assert args[1] == ["backup.example"]
    assert (kwargs["username"], kwargs["password"], kwargs["key_filepath"]) == (
        "backup",
        "from-config",
        "/keys/id_ed25519",
    )
    assert kwargs["encryption_key"] is None
