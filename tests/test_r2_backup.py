from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.upload_r2_backup import (
    ACCOUNT_ID,
    Archive,
    AwsTransport,
    BackupError,
    inspect_archive,
    upload,
    validate_destination,
)

ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    # Arbitrary bytes deliberately test transport integrity, not encryption.
    path = tmp_path / "vpn-test.tar.gz.gpg"
    body = bytes(range(256)) * 4096
    path.write_bytes(body)
    sha = tmp_path / (path.name + ".sha256")
    sha.write_text(f"{hashlib.sha256(body).hexdigest()}  {path.name}\n")
    return inspect_archive(path, sha)


@pytest.fixture
def transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AwsTransport:
    binary = tmp_path / "fake-aws"
    binary.write_text(
        f"#!{sys.executable}\n"
        + r"""
import os
import pathlib
import sys
import time

args = sys.argv[1:]
root = pathlib.Path(os.environ["R2_TEST_STORE"])
mode = os.environ.get("R2_TEST_MODE", "ok")
if "put-object" in args:
    if mode == "upload_error":
        sys.stderr.write("sensitive provider diagnostic must not be relayed")
        sys.exit(8)
    key = args[args.index("--key") + 1]
    body = pathlib.Path(args[args.index("--body") + 1])
    output = root / key
    output.parent.mkdir(parents=True, exist_ok=True)
    if "--if-none-match" not in args or args[args.index("--if-none-match")+1] != "*":
        sys.exit(9)
    with output.open("xb") as stream:
        stream.write(body.read_bytes())
elif "cp" in args:
    if mode == "timeout":
        time.sleep(30)
    source = args[args.index("cp") + 1]
    key = source.split("/", 3)[3]
    data = (root / key).read_bytes()
    if mode == "corrupt":
        data = b"x" + data[1:]
    elif mode == "truncated":
        data = data[:-10]
    elif mode == "oversized":
        data += b"x" * 1048576
    sys.stdout.buffer.write(data)
    if mode == "download_error":
        sys.exit(10)
else:
    sys.exit(11)
"""
    )
    binary.chmod(0o700)
    monkeypatch.setenv("R2_TEST_STORE", str(tmp_path / "objects"))
    return AwsTransport(ENDPOINT, "cyberdefency-r2", 10, executable=str(binary))


async def test_roundtrip_verification_creates_private_receipt_without_removing_source(
    archive: Archive, transport: AwsTransport
) -> None:
    receipt = await upload(archive, transport, bucket="test-backups", endpoint=ENDPOINT)
    data = json.loads(receipt.read_text())
    assert data["sha256"] == archive.sha256
    assert data["size_bytes"] == archive.size
    assert data["verification"] == "full_ciphertext_readback_sha256"
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert archive.path.is_file()
    # Repeating an upload gets a separate remote object and separate receipt.
    second = await upload(archive, transport, bucket="test-backups", endpoint=ENDPOINT)
    assert json.loads(second.read_text())["object_key"] != data["object_key"]


@pytest.mark.parametrize(
    "mode", ["upload_error", "corrupt", "truncated", "oversized", "download_error"]
)
async def test_failures_never_publish_success_receipt_or_remove_archive(
    archive: Archive, transport: AwsTransport, tmp_path: Path, mode: str
) -> None:
    transport.environment["R2_TEST_MODE"] = mode
    receipt = tmp_path / "verified.json"
    with pytest.raises(BackupError):
        await upload(
            archive,
            transport,
            bucket="test-backups",
            endpoint=ENDPOINT,
            receipt=receipt,
        )
    assert not receipt.exists()
    assert archive.path.is_file()
    assert not list(tmp_path.glob(".r2-receipt-*"))


async def test_download_timeout_kills_child_and_leaves_source(
    archive: Archive, transport: AwsTransport, tmp_path: Path
) -> None:
    transport.environment["R2_TEST_MODE"] = "timeout"
    transport.timeout = 1
    receipt = tmp_path / "verified.json"
    with pytest.raises(TimeoutError):
        await upload(
            archive,
            transport,
            bucket="test-backups",
            endpoint=ENDPOINT,
            receipt=receipt,
        )
    assert not receipt.exists()
    assert archive.path.exists()


def test_changed_archive_is_rejected_before_upload(archive: Archive) -> None:
    archive.path.write_bytes(b"changed")
    with pytest.raises(BackupError, match="CHECKSUM_MISMATCH"):
        inspect_archive(archive.path, Path(str(archive.path) + ".sha256"))


def test_checksum_must_name_the_selected_archive(
    archive: Archive, tmp_path: Path
) -> None:
    sha = tmp_path / "wrong.sha256"
    sha.write_text(f"{archive.sha256}  another.tar.gz.gpg\n")
    with pytest.raises(BackupError, match="NAME_THIS_ARCHIVE"):
        inspect_archive(archive.path, sha)
    sha.write_text(f"{archive.sha256}  {archive.path}\n")
    assert inspect_archive(archive.path, sha).sha256 == archive.sha256


@pytest.mark.parametrize("name", ["recovery-key", "backup.tar.gz", "secret.txt"])
def test_recovery_keys_and_plain_archives_are_not_accepted(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / name
    path.write_bytes(b"not-an-encrypted-archive")
    with pytest.raises(BackupError, match="ENCRYPTED_ARCHIVE_REQUIRED"):
        inspect_archive(path, tmp_path / "unused.sha256")


async def test_existing_receipt_is_never_replaced(
    archive: Archive, transport: AwsTransport, tmp_path: Path
) -> None:
    receipt = tmp_path / "already-verified.json"
    receipt.write_text("keep")
    with pytest.raises(BackupError, match="RECEIPT_ALREADY_EXISTS"):
        await upload(
            archive,
            transport,
            bucket="test-backups",
            endpoint=ENDPOINT,
            receipt=receipt,
        )
    assert receipt.read_text() == "keep"
    assert not (tmp_path / "objects").exists()


def test_destination_is_restricted_to_r2_account_endpoint() -> None:
    assert (
        validate_destination(ACCOUNT_ID, "test-backups", "cyberdefency-r2", "vpn")
        == ENDPOINT
    )
    with pytest.raises(BackupError, match="ACCOUNT_ID"):
        validate_destination("user@example.com", "test-backups", "r2", "vpn")
    with pytest.raises(BackupError, match="BUCKET_NAME"):
        validate_destination(ACCOUNT_ID, "127.0.0.1", "r2", "vpn")
    with pytest.raises(BackupError, match="OBJECT_PREFIX"):
        validate_destination(ACCOUNT_ID, "test-backups", "r2", "../secret")
