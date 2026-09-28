"""Tests for S3 sync (boto3 client mocked)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from botocore.exceptions import ClientError

from src.encryption import EncryptionKey
from src.s3_sync import sync_to_s3


def _client():
    client = mock.Mock()
    client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    return client


def _uploads(client):
    return {c.args[2]: c for c in client.upload_file.call_args_list}


class TestSyncToS3:
    def test_plain_upload_keys(self, tmp_dir, logger):
        (tmp_dir / "sub").mkdir()
        (tmp_dir / "sub" / "a.txt").write_text("A")
        client = _client()
        with mock.patch("boto3.client", return_value=client):
            assert sync_to_s3(logger, str(tmp_dir), "bucket", prefix="daily/")
        assert list(_uploads(client)) == ["daily/sub/a.txt"]

    def test_client_side_encryption_uploads_ciphertext_only(self, tmp_dir, logger):
        (tmp_dir / "a.txt").write_text("secret")
        client = _client()
        seen = {}

        def capture(path, bucket, key, **kwargs):
            with open(path, "rb") as f:
                seen[key] = f.read()

        client.upload_file.side_effect = capture
        with mock.patch("boto3.client", return_value=client):
            assert sync_to_s3(logger, str(tmp_dir), "b", encryption_key=EncryptionKey.create(passphrase="pw"))
        assert list(seen) == ["a.txt.enc"]
        assert b"secret" not in seen["a.txt.enc"]

    def test_server_side_encryption_args(self, tmp_dir, logger):
        (tmp_dir / "a.txt").write_text("A")
        client = _client()
        with mock.patch("boto3.client", return_value=client):
            sync_to_s3(
                logger, str(tmp_dir), "b", server_side_encryption="aws:kms", sse_kms_key_id="alias/backup"
            )
        kwargs = client.upload_file.call_args.kwargs
        assert kwargs["ExtraArgs"] == {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "alias/backup"}

    def test_incremental_skips_objects_newer_than_local(self, tmp_dir, logger):
        (tmp_dir / "a.txt").write_text("A")
        client = mock.Mock()
        client.head_object.return_value = {"LastModified": datetime(2999, 1, 1, tzinfo=timezone.utc)}
        with mock.patch("boto3.client", return_value=client):
            assert sync_to_s3(logger, str(tmp_dir), "b", mode="incremental")
        client.upload_file.assert_not_called()

    def test_upload_failure_returns_false(self, tmp_dir, logger):
        (tmp_dir / "a.txt").write_text("A")
        client = _client()
        client.upload_file.side_effect = OSError("network down")
        with mock.patch("boto3.client", return_value=client):
            assert sync_to_s3(logger, str(tmp_dir), "b") is False
