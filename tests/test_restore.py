"""Tests for remote-path parsing used by the restore pipeline."""

from __future__ import annotations

from src.restore import _is_s3_path, _is_ssh_path, _parse_s3_path, _parse_ssh_path


class TestRemoteRestore:
    def test_is_ssh_path(self):
        assert _is_ssh_path("user@host:/backup/dir") is True
        assert _is_ssh_path("ssh://user@host/backup") is True
        assert _is_ssh_path("/local/path") is False
        assert _is_ssh_path("s3://bucket/prefix") is False

    def test_is_s3_path(self):
        assert _is_s3_path("s3://my-bucket/prefix") is True
        assert _is_s3_path("s3://bucket") is True
        assert _is_s3_path("/local/path") is False
        assert _is_s3_path("user@host:/path") is False

    def test_parse_ssh_path_user_host(self):
        user, host, path = _parse_ssh_path("admin@backup-server:/backups/daily")
        assert user == "admin"
        assert host == "backup-server"
        assert path == "/backups/daily"

    def test_parse_ssh_path_no_user(self):
        user, host, path = _parse_ssh_path("backup-server:/backups/daily")
        assert user is None
        assert host == "backup-server"
        assert path == "/backups/daily"

    def test_parse_ssh_url(self):
        user, host, path = _parse_ssh_path("ssh://admin@backup-server/backups/daily")
        assert user == "admin"
        assert host == "backup-server"
        assert path == "/backups/daily"

    def test_parse_s3_path(self):
        bucket, prefix = _parse_s3_path("s3://my-bucket/backups/2026")
        assert bucket == "my-bucket"
        assert prefix == "backups/2026"

    def test_parse_s3_path_no_prefix(self):
        bucket, prefix = _parse_s3_path("s3://my-bucket")
        assert bucket == "my-bucket"
        assert prefix == ""

    def test_parse_s3_path_single_prefix(self):
        bucket, prefix = _parse_s3_path("s3://bucket/prefix")
        assert bucket == "bucket"
        assert prefix == "prefix"


class TestRestorePathSafety:
    def test_sftp_download_skips_traversal_names(self, logger, tmp_dir):
        import stat
        from types import SimpleNamespace
        from unittest import mock

        from src.restore import _sftp_download_recursive

        sftp = mock.Mock()
        sftp.listdir_attr.return_value = [
            SimpleNamespace(filename="../evil", st_mode=stat.S_IFREG),
            SimpleNamespace(filename="..", st_mode=stat.S_IFDIR),
            SimpleNamespace(filename="good.txt", st_mode=stat.S_IFREG),
        ]
        _sftp_download_recursive(sftp, "/remote", str(tmp_dir / "out"), logger)
        fetched = [call.args[1] for call in sftp.get.call_args_list]
        assert fetched == [str(tmp_dir / "out" / "good.txt")]

    def test_s3_download_skips_keys_escaping_target(self, logger, tmp_dir):
        from unittest import mock

        from src.restore import _download_from_s3

        client = mock.Mock()
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "backups/../../escape.txt"}, {"Key": "backups/ok.txt"}]}
        ]
        with mock.patch("boto3.client", return_value=client):
            assert _download_from_s3(logger, "s3://bucket/backups", str(tmp_dir / "out"))
        keys = [call.args[1] for call in client.download_file.call_args_list]
        assert keys == ["backups/ok.txt"]

    def test_manifest_restore_never_reads_outside_backup(self, logger, tmp_dir):
        from unittest import mock

        from src.restore import _restore_with_manifests

        backup = tmp_dir / "backup"
        backup.mkdir()
        outside = tmp_dir / "secret.txt"
        outside.write_text("host secret")
        dest = tmp_dir / "dest"
        manifests = [{"copied": [{"path": str(outside)}]}]
        with mock.patch("src.restore.load_manifests_up_to", return_value=manifests):
            ok = _restore_with_manifests(logger, backup, dest, "20990101_000000")
        assert ok is False
        assert not (dest / "secret.txt").exists()

    def test_manifest_restore_picks_file_with_matching_parent(self, logger, tmp_dir):
        from unittest import mock

        from src.restore import _restore_with_manifests

        backup = tmp_dir / "backup"
        (backup / "a").mkdir(parents=True)
        (backup / "b").mkdir(parents=True)
        (backup / "a" / "config.ini").write_text("A")
        (backup / "b" / "config.ini").write_text("B")
        manifests = [{"copied": [{"path": "/srv/app/b/config.ini"}]}]
        with mock.patch("src.restore.load_manifests_up_to", return_value=manifests):
            assert _restore_with_manifests(logger, backup, tmp_dir / "dest", "20990101_000000")
        assert (tmp_dir / "dest" / "b" / "config.ini").read_text() == "B"
        assert not (tmp_dir / "dest" / "a").exists()
