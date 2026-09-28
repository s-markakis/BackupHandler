"""
s3_sync.py - AWS S3 Cloud Backup Synchronization

Uploads local backup files to an AWS S3 bucket with support for full,
incremental, and differential modes. In incremental/differential mode,
compares local modification times against S3 object timestamps to skip
unchanged files. Displays a progress bar via ``tqdm`` during upload.
"""

from pathlib import Path

from tqdm import tqdm

from .encryption import remote_name, upload_source
from .utils import calculate_checksum, should_exclude


def sync_to_s3(
    logger,
    source_dir,
    bucket,
    prefix="",
    region=None,
    access_key=None,
    secret_key=None,
    mode="full",
    exclude_patterns=None,
    manifest=None,
    max_bandwidth=None,
    multipart_threshold=None,
    max_concurrency=None,
    encryption_key=None,
    server_side_encryption=None,
    sse_kms_key_id=None,
):
    """
    Sync a local directory to an S3 bucket.

    Parameters:
    - logger: Logger instance.
    - source_dir (str): Local directory to sync.
    - bucket (str): S3 bucket name.
    - prefix (str): S3 key prefix (folder path in bucket).
    - region (str): AWS region.
    - access_key (str, optional): AWS access key. Uses default credentials if not set.
    - secret_key (str, optional): AWS secret key. Uses default credentials if not set.
    - mode (str): Backup mode ('full', 'incremental', 'differential').
    - exclude_patterns (list of str, optional): Glob patterns to exclude.
    - manifest (BackupManifest, optional): Manifest to record operations.
    - encryption_key (EncryptionKey, optional): Encrypt each file client-side
      and upload it as ``<key>.enc`` — plaintext never reaches S3.
    - server_side_encryption (str, optional): ``AES256`` or ``aws:kms``.
    - sse_kms_key_id (str, optional): KMS key for ``aws:kms``.

    Returns:
    - bool: True if sync completed successfully.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        logger.error("boto3 is not installed. Install it with: pip install boto3")
        return False

    source_path = Path(source_dir)
    if not source_path.exists():
        logger.error(f"Source directory does not exist: {source_dir}")
        return False

    # Create S3 client
    session_kwargs = {}
    if region:
        session_kwargs["region_name"] = region
    if access_key and secret_key:
        session_kwargs["aws_access_key_id"] = access_key
        session_kwargs["aws_secret_access_key"] = secret_key

    s3 = boto3.client("s3", **session_kwargs)

    # Configure transfer settings for bandwidth and multipart uploads
    from boto3.s3.transfer import TransferConfig

    transfer_kwargs = {}
    if max_bandwidth:
        transfer_kwargs["max_bandwidth"] = max_bandwidth * 1024  # KB/s -> bytes/s
    if multipart_threshold:
        transfer_kwargs["multipart_threshold"] = multipart_threshold * 1024 * 1024  # MB -> bytes
    if max_concurrency:
        transfer_kwargs["max_concurrency"] = max_concurrency
    transfer_config = TransferConfig(**transfer_kwargs) if transfer_kwargs else None

    extra_args = {}
    if server_side_encryption:
        extra_args["ServerSideEncryption"] = server_side_encryption
        if server_side_encryption == "aws:kms" and sse_kms_key_id:
            extra_args["SSEKMSKeyId"] = sse_kms_key_id

    # Collect files to upload
    files = [
        f
        for f in source_path.rglob("*")
        if f.is_file() and not should_exclude(f.relative_to(source_path), exclude_patterns)
    ]

    logger.info(f"Syncing {len(files)} files to s3://{bucket}/{prefix}")

    uploaded = 0
    skipped = 0
    failed = 0

    for local_file in tqdm(files, desc=f"Uploading to s3://{bucket}/{prefix}", unit="files"):
        relative = remote_name(local_file.relative_to(source_path).as_posix(), encryption_key)
        s3_key = f"{prefix.rstrip('/')}/{relative}" if prefix else relative

        should_upload = True

        if mode in ("incremental", "differential"):
            try:
                response = s3.head_object(Bucket=bucket, Key=s3_key)
                remote_mtime = response["LastModified"].timestamp()
                local_mtime = local_file.stat().st_mtime
                should_upload = local_mtime > remote_mtime
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    should_upload = True
                else:
                    logger.error(f"Error checking S3 object {s3_key}: {e}")
                    failed += 1
                    if manifest:
                        manifest.record_failure(str(local_file), str(e))
                    continue

        if should_upload:
            try:
                upload_kwargs = {}
                if transfer_config:
                    upload_kwargs["Config"] = transfer_config
                if extra_args:
                    upload_kwargs["ExtraArgs"] = extra_args
                with upload_source(local_file, encryption_key) as src:
                    s3.upload_file(str(src), bucket, s3_key, **upload_kwargs)
                logger.info(f"Uploaded {local_file} -> s3://{bucket}/{s3_key}")
                uploaded += 1
                if manifest:
                    checksum = calculate_checksum(str(local_file))
                    manifest.record_copy(str(local_file), local_file.stat().st_size, checksum=checksum)
            except Exception as e:
                logger.error(f"Failed to upload {local_file} to S3: {e}")
                failed += 1
                if manifest:
                    manifest.record_failure(str(local_file), str(e))
        else:
            skipped += 1
            if manifest:
                manifest.record_skip(str(local_file))

    logger.info(f"S3 sync complete: {uploaded} uploaded, {skipped} skipped, {failed} failed")
    return failed == 0
