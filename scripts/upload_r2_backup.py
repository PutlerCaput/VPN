#!/usr/bin/env python3
"""Upload an existing encrypted archive to private R2 and verify a full readback.

Needs a recent AWS CLI v2 and a bucket-scoped profile. Never uploads recovery
keys, removes originals, or considers S3 ETags to be SHA256 checksums.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ACCOUNT_ID = "c2d2ec473f1773d31bb77552c6faccdb"
CHUNK_SIZE = 1024 * 1024
# Single PUT supports a conditional create; multipart uploads need a different
# commit protocol. Refuse larger archives instead of silently losing that guard.
MAX_ARCHIVE_SIZE = 5 * 1024**3
ENCRYPTED_SUFFIXES = (".tar.gz.gpg", ".tar.gz.enc", ".db.gpg")


class BackupError(RuntimeError):
    """Safe operational error: contains no provider response or credential."""


@dataclass(frozen=True)
class Archive:
    path: Path
    sha256: str
    size: int
    identity: tuple[int, int, int, int]


def file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BackupError("ARCHIVE_MUST_BE_REGULAR_FILE")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_archive(path: Path, checksum_path: Path) -> Archive:
    if path.is_symlink() or not path.name.endswith(ENCRYPTED_SUFFIXES):
        raise BackupError("ENCRYPTED_ARCHIVE_REQUIRED")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,220}", path.name):
        raise BackupError("UNSAFE_ARCHIVE_NAME")
    path = path.resolve(strict=True)
    identity = file_identity(path)
    if not 0 < identity[2] <= MAX_ARCHIVE_SIZE:
        raise BackupError("ARCHIVE_SIZE_OUT_OF_RANGE_MAX_5_GIB")
    if checksum_path.stat().st_size > 4096:
        raise BackupError("CHECKSUM_FILE_TOO_LARGE")
    text = checksum_path.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)", text)
    if match is None or match[2] not in (path.name, str(path)):
        raise BackupError("CHECKSUM_MUST_NAME_THIS_ARCHIVE_EXACTLY")
    expected = match[1].lower()
    if checksum(path) != expected or file_identity(path) != identity:
        raise BackupError("LOCAL_CHECKSUM_MISMATCH_OR_FILE_CHANGED")
    return Archive(path, expected, identity[2], identity)


def validate_destination(
    account_id: str, bucket: str, profile: str, prefix: str
) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", account_id):
        raise BackupError("INVALID_R2_ACCOUNT_ID")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or any(
        part in bucket for part in ("..", ".-", "-.")
    ):
        raise BackupError("INVALID_BUCKET_NAME")
    try:
        ipaddress.ip_address(bucket)
    except ValueError:
        pass
    else:
        raise BackupError("INVALID_BUCKET_NAME")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
        raise BackupError("INVALID_PROFILE_NAME")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,120}", prefix)
        or "//" in prefix
        or prefix.endswith("/")
    ):
        raise BackupError("INVALID_OBJECT_PREFIX")
    return f"https://{account_id}.r2.cloudflarestorage.com"


class AwsTransport:
    def __init__(
        self,
        endpoint: str,
        profile: str,
        timeout: int,
        *,
        executable: str = "aws",
    ) -> None:
        binary = shutil.which(executable)
        if binary is None:
            raise BackupError("AWS_CLI_NOT_FOUND")
        if not 1 <= timeout <= 8 * 3600:
            raise BackupError("INVALID_TIMEOUT")
        self.timeout = timeout
        self.base = [
            binary,
            "--profile",
            profile,
            "--endpoint-url",
            endpoint,
            "--region",
            "auto",
            "--no-cli-pager",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "60",
        ]
        # Let AWS CLI read the selected profile itself. Never read or log keys.
        self.environment = {
            **os.environ,
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        }

    async def put(self, archive: Archive, bucket: str, object_key: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *self.base,
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            object_key,
            "--body",
            str(archive.path),
            "--if-none-match",
            "*",
            "--metadata",
            f"sha256={archive.sha256}",
            "--content-type",
            "application/octet-stream",
            env=self.environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(self.timeout):
                if await process.wait() != 0:
                    raise BackupError("R2_UPLOAD_FAILED_CHECK_PROFILE_AND_PERMISSIONS")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def verify(self, archive: Archive, bucket: str, object_key: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *self.base,
            "s3",
            "cp",
            f"s3://{bucket}/{object_key}",
            "-",
            "--only-show-errors",
            "--no-progress",
            env=self.environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        digest = hashlib.sha256()
        length = 0
        try:
            async with asyncio.timeout(self.timeout):
                if process.stdout is None:
                    raise BackupError("DOWNLOAD_STREAM_UNAVAILABLE")
                while chunk := await process.stdout.read(CHUNK_SIZE):
                    length += len(chunk)
                    if length > archive.size:
                        raise BackupError("R2_READBACK_SIZE_MISMATCH")
                    digest.update(chunk)
                if await process.wait() != 0:
                    raise BackupError("R2_READBACK_FAILED")
            if length != archive.size or digest.hexdigest() != archive.sha256:
                raise BackupError("R2_READBACK_CHECKSUM_MISMATCH")
        finally:
            if process.returncode is None:
                process.kill()
            # Drain only the finished child's remaining pipe buffers so wait
            # cannot hang behind a paused stdout reader after a mismatch.
            if process.stdout is not None:
                while await process.stdout.read(CHUNK_SIZE):
                    pass
            await process.wait()


def write_receipt(path: Path, payload: dict[str, Any]) -> None:
    """Atomically publish a private receipt without replacing an existing one."""
    descriptor, temporary = tempfile.mkstemp(prefix=".r2-receipt-", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, path)  # Atomic, fails if destination already exists.
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


async def upload(
    archive: Archive,
    transport: AwsTransport,
    *,
    bucket: str,
    endpoint: str,
    prefix: str = "vpn-backups",
    receipt: Path | None = None,
) -> Path:
    current = datetime.now(UTC)
    identifier = uuid.uuid4().hex
    object_key = f"{prefix}/{current:%Y/%m/%d}/{identifier}/{archive.path.name}"
    receipt = receipt or archive.path.with_name(
        f"{archive.path.name}.r2-{identifier}.verified.json"
    )
    if receipt.exists() or receipt.is_symlink():
        raise BackupError("RECEIPT_ALREADY_EXISTS")
    if file_identity(archive.path) != archive.identity:
        raise BackupError("LOCAL_ARCHIVE_CHANGED")
    await transport.put(archive, bucket, object_key)
    await transport.verify(archive, bucket, object_key)
    if file_identity(archive.path) != archive.identity:
        raise BackupError("LOCAL_ARCHIVE_CHANGED")
    write_receipt(
        receipt,
        {
            "version": 1,
            "verified_at_utc": datetime.now(UTC).isoformat(),
            "endpoint": endpoint,
            "bucket": bucket,
            "object_key": object_key,
            "archive_name": archive.path.name,
            "size_bytes": archive.size,
            "sha256": archive.sha256,
            "verification": "full_ciphertext_readback_sha256",
        },
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--checksum", type=Path)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--account-id", default=ACCOUNT_ID)
    parser.add_argument("--profile", default="cyberdefency-r2")
    parser.add_argument("--prefix", default="vpn-backups")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        endpoint = validate_destination(
            args.account_id, args.bucket, args.profile, args.prefix
        )
        sha_file = args.checksum or Path(str(args.archive) + ".sha256")
        archive = inspect_archive(args.archive, sha_file)
        transport = AwsTransport(endpoint, args.profile, args.timeout)
        receipt = asyncio.run(
            upload(
                archive,
                transport,
                bucket=args.bucket,
                endpoint=endpoint,
                prefix=args.prefix,
                receipt=args.receipt,
            )
        )
    except BackupError as exc:
        print(f"R2_BACKUP_FAILED={exc}", file=sys.stderr)
        return 1
    except (OSError, TimeoutError, UnicodeError):
        print("R2_BACKUP_FAILED=IO_OR_TIMEOUT_ERROR", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("R2_BACKUP_FAILED=INTERRUPTED", file=sys.stderr)
        return 130
    print(f"VERIFIED_R2_RECEIPT={receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
