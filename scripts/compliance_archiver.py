#!/usr/bin/env python3
"""Archive confirmed AUTOBAN evidence from the actual SQLite/SQLCipher schema.

Never deletes a row until its encrypted archive has been durably stored and
successfully decrypted with an identical JSONL SHA256 digest.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess  # nosec B404
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger("compliance_archiver")
RETENTION_SECONDS = 30 * 86400
CHUNK = 1024 * 1024
MAX_ROW_BYTES = 2 * CHUNK
TABLES = {
    "anomalies": ("id", "telegram_id", "node_username", "kind", "detail", "created_at"),
    "audit_log": ("id", "telegram_id", "action", "target", "detail", "created_at"),
}
SELECTS = {
    "anomalies": "SELECT * FROM anomalies WHERE kind = 'AUTOBAN' AND created_at < ?",
    "audit_log": "SELECT * FROM audit_log WHERE action = 'autoban' AND created_at < ?",
}
DELETES = {
    "anomalies": (
        "DELETE FROM anomalies WHERE id IS ? AND telegram_id IS ? "
        "AND node_username IS ? AND kind IS ? AND detail IS ? AND created_at IS ? "
        "AND kind = 'AUTOBAN' AND created_at < ?"
    ),
    "audit_log": (
        "DELETE FROM audit_log WHERE id IS ? AND telegram_id IS ? "
        "AND action IS ? AND target IS ? AND detail IS ? AND created_at IS ? "
        "AND action = 'autoban' AND created_at < ?"
    ),
}


class ArchiveError(RuntimeError):
    pass


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_secret(path: Path | None, variable: str) -> str:
    if path is None:
        return os.environ.get(variable, "")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ArchiveError("secret_file_requires_mode_600")
        value = source.read(4097)
    if len(value) > 4096:
        raise ArchiveError("secret_too_long")
    return value.rstrip("\r\n")


def connect(path: Path, key: str | None, *, readonly: bool) -> Any:
    if not path.is_file():
        raise ArchiveError("database_not_found")
    module: Any = sqlite3
    if key:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ArchiveError("invalid_database_key")
        from sqlcipher3 import dbapi2 as sqlcipher

        module = sqlcipher
    connection = module.connect(
        path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
        uri=True,
        timeout=5,
    )
    try:
        if key:
            connection.execute(f"PRAGMA key = \"x'{key}'\"")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA query_only=" + ("ON" if readonly else "OFF"))
        for table, columns in TABLES.items():
            found = connection.execute(f"PRAGMA table_info({table})").fetchall()
            if tuple(row[1] for row in found) != columns or found[0][5] != 1:
                raise ArchiveError("unsupported_compliance_schema")
        connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def export_rows(
    database: Path, db_key: str | None, destination: Path, cutoff: int
) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    connection = connect(database, db_key, readonly=True)
    try:
        connection.execute("BEGIN")
        with destination.open("xb") as output:
            destination.chmod(0o600)
            for table in TABLES:
                # Abort before fetching an oversized detail into Python memory.
                oversized = connection.execute(
                    SELECTS[table].replace("SELECT *", "SELECT 1")
                    + " AND length(CAST(detail AS BLOB)) > ? LIMIT 1",
                    (cutoff, CHUNK),
                ).fetchone()
                if oversized:
                    raise ArchiveError("evidence_row_exceeds_size_limit")
                rows = connection.execute(
                    SELECTS[table] + " ORDER BY id",
                    (cutoff,),
                )
                for row in rows:
                    line = (
                        json.dumps(
                            {"table": table, "row": list(row)},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode()
                    if len(line) > MAX_ROW_BYTES:
                        raise ArchiveError("evidence_row_exceeds_size_limit")
                    output.write(line)
                    digest.update(line)
                    count += 1
            output.flush()
            os.fsync(output.fileno())
        connection.rollback()
    finally:
        connection.close()
    return {
        "format": 1,
        "cutoff_utc_epoch": cutoff,
        "rows": count,
        "sha256": digest.hexdigest(),
    }


@contextlib.contextmanager
def gpg_process(
    args: list[str], key: str, home: Path, *, stdin: Any, stdout: Any, timeout: int
) -> Iterator[subprocess.Popen[bytes]]:
    executable = shutil.which("gpg")
    if executable is None:
        raise ArchiveError("gpg_not_installed")
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, key.encode() + b"\n")
    finally:
        os.close(write_fd)
    try:
        process = subprocess.Popen(  # noqa: S603 # nosec B603
            [
                executable,
                "--homedir",
                str(home),
                "--batch",
                "--no-tty",
                "--pinentry-mode",
                "loopback",
                "--no-symkey-cache",
                "--passphrase-fd",
                str(read_fd),
                *args,
            ],
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            pass_fds=(read_fd,),
        )
    finally:
        os.close(read_fd)
    watchdog = threading.Timer(timeout, process.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield process
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()


def encrypt(
    source: Path,
    destination: Path,
    manifest: dict[str, Any],
    key: str,
    home: Path,
    timeout: int,
) -> None:
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    with destination.open("xb") as output:
        destination.chmod(0o600)
        with gpg_process(
            [
                "--symmetric",
                "--cipher-algo",
                "AES256",
                "--s2k-mode",
                "3",
                "--s2k-digest-algo",
                "SHA256",
                "--s2k-count",
                "65011712",
                "--compress-algo",
                "none",
                "--output",
                "-",
            ],
            key,
            home,
            stdin=subprocess.PIPE,
            stdout=output,
            timeout=timeout,
        ) as process:
            if process.stdin is None:
                raise ArchiveError("gpg_input_missing")
            with tarfile.open(fileobj=process.stdin, mode="w|gz") as archive:
                entry = tarfile.TarInfo("evidence.jsonl")
                entry.size = source.stat().st_size
                entry.mode = 0o600
                with source.open("rb") as data:
                    archive.addfile(entry, data)
                entry = tarfile.TarInfo("manifest.json")
                entry.size = len(manifest_bytes)
                entry.mode = 0o600
                archive.addfile(entry, io.BytesIO(manifest_bytes))
            process.stdin.close()
            if process.wait(timeout=timeout) != 0:
                raise ArchiveError("encryption_failed")
        output.flush()
        os.fsync(output.fileno())


def verify_archive(
    path: Path, manifest: dict[str, Any], key: str, home: Path, timeout: int
) -> None:
    digest = hashlib.sha256()
    found: list[str] = []
    recovered_manifest: Any = None
    with (
        path.open("rb") as source,
        gpg_process(
            ["--decrypt", "--output", "-"],
            key,
            home,
            stdin=source,
            stdout=subprocess.PIPE,
            timeout=timeout,
        ) as process,
    ):
        if process.stdout is None:
            raise ArchiveError("gpg_output_missing")
        with tarfile.open(fileobj=process.stdout, mode="r|gz") as archive:
            for entry in archive:
                if (
                    not entry.isfile()
                    or entry.name not in {"evidence.jsonl", "manifest.json"}
                    or entry.name in found
                ):
                    raise ArchiveError("unexpected_archive_member")
                found.append(entry.name)
                stream = archive.extractfile(entry)
                if stream is None:
                    raise ArchiveError("archive_member_missing")
                if entry.name == "manifest.json":
                    if entry.size > 4096:
                        raise ArchiveError("invalid_archive_manifest")
                    recovered_manifest = json.loads(stream.read())
                else:
                    while chunk := stream.read(CHUNK):
                        digest.update(chunk)
        # GPG authentication completes at EOF; a readable tar is insufficient.
        while process.stdout.read(CHUNK):
            pass
        if process.wait(timeout=timeout) != 0:
            raise ArchiveError("decryption_authentication_failed")
    if (
        found != ["evidence.jsonl", "manifest.json"]
        or recovered_manifest != manifest
        or digest.hexdigest() != manifest["sha256"]
    ):
        raise ArchiveError("archive_checksum_mismatch")


def evidence_rows(stream: BinaryIO) -> Iterator[dict[str, Any]]:
    while line := stream.readline(MAX_ROW_BYTES + 1):
        if len(line) > MAX_ROW_BYTES or not line.endswith(b"\n"):
            raise ArchiveError("invalid_evidence_line")
        item: Any = json.loads(line)
        if not isinstance(item, dict) or item.get("table") not in TABLES:
            raise ArchiveError("invalid_evidence_table")
        table = item["table"]
        row = item.get("row")
        if not isinstance(row, list) or len(row) != len(TABLES[table]):
            raise ArchiveError("invalid_evidence_row")
        yield item


def purge_rows(
    database: Path,
    db_key: str | None,
    source: Path,
    manifest: dict[str, Any],
    batch_size: int = 250,
) -> int:
    if digest_file(source) != manifest["sha256"]:
        raise ArchiveError("export_changed_before_purge")
    connection = connect(database, db_key, readonly=False)
    deleted = 0
    batch = 0
    try:
        connection.execute("PRAGMA secure_delete=ON")
        with source.open("rb") as stream:
            for item in evidence_rows(stream):
                if batch == 0:
                    connection.execute("BEGIN IMMEDIATE")
                table = item["table"]
                cursor = connection.execute(
                    DELETES[table],
                    (*item["row"], manifest["cutoff_utc_epoch"]),
                )
                deleted += cursor.rowcount
                batch += 1
                if batch >= batch_size:
                    connection.commit()
                    batch = 0
            connection.commit()
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        connection.execute("PRAGMA optimize")
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return deleted


def run(
    database: Path,
    cold_dir: Path,
    key: str,
    *,
    db_key: str | None = None,
    now: int | None = None,
    timeout: int = 3600,
    temp_dir: Path | None = None,
) -> dict[str, Any]:
    if not 32 <= len(key.encode()) <= 4096 or any(char in key for char in "\n\r\x00"):
        raise ArchiveError("archive_key_requires_32_to_4096_bytes")
    if not cold_dir.is_dir():
        raise ArchiveError("cold_directory_must_exist")
    if cold_dir.is_symlink() or cold_dir.stat().st_mode & 0o077:
        raise ArchiveError("cold_directory_requires_mode_700")
    timestamp = now if now is not None else int(time.time())
    cutoff = timestamp - RETENTION_SECONDS
    with (cold_dir / ".archiver.lock").open("a+b") as lock:
        os.chmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveError("another_archiver_is_running") from None
        with tempfile.TemporaryDirectory(prefix="compliance-", dir=temp_dir) as temp:
            working = Path(temp)
            source = working / "evidence.jsonl"
            manifest = export_rows(database, db_key, source, cutoff)
            if manifest["rows"] == 0:
                return {"status": "nothing_to_archive", "rows": 0}
            home = working / "gnupg"
            home.mkdir(mode=0o700)
            stamp = datetime.fromtimestamp(timestamp, UTC).strftime("%Y_%m_%d")
            directory = cold_dir / (stamp + "_" + uuid.uuid4().hex)
            directory.mkdir(mode=0o700)
            fsync_dir(cold_dir)
            final = directory / f"compliance_archive_{stamp}.tar.gz.enc"
            partial = directory / ".archive.partial"
            encrypt(source, partial, manifest, key, home, timeout)
            encrypted_digest = digest_file(partial)
            verify_archive(partial, manifest, key, home, timeout)
            if digest_file(partial) != encrypted_digest:
                raise ArchiveError("encrypted_archive_changed")
            partial.rename(final)
            with final.with_suffix(final.suffix + ".sha256").open(
                "x", encoding="utf-8"
            ) as checksum:
                checksum.write(encrypted_digest + "  " + final.name + "\n")
                checksum.flush()
                os.fsync(checksum.fileno())
            fsync_dir(directory)
            deleted = purge_rows(database, db_key, source, manifest)
            result = {
                "status": "archived",
                "rows": manifest["rows"],
                "deleted": deleted,
                "archive": str(final),
                "sha256": encrypted_digest,
            }
            with (directory / "completed.json").open("x", encoding="utf-8") as receipt:
                json.dump(result, receipt, sort_keys=True)
                receipt.flush()
                os.fsync(receipt.fileno())
            fsync_dir(directory)
            return result


def alert() -> None:
    url = os.environ.get("COMPLIANCE_ALERT_WEBHOOK", "")
    if not url:
        return
    if not url.startswith("https://"):
        logger.error("alert_requires_https")
        return
    try:
        request = urllib.request.Request(  # noqa: S310 -- configured HTTPS alert receiver
            url,
            data=b'{"priority":"high","event":"compliance_archive_failed"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310 # nosec B310
            pass
    except Exception:
        logger.error("alert_delivery_failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cold-dir", type=Path, required=True)
    parser.add_argument("--archive-key-file", type=Path)
    parser.add_argument("--db-key-file", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    try:
        key = private_secret(args.archive_key_file, "COMPLIANCE_ARCHIVE_KEY")
        db_key = private_secret(args.db_key_file, "DB_KEY") or None
        result = run(
            args.database, args.cold_dir, key, db_key=db_key, temp_dir=args.temp_dir
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        logger.error("archive_failed error_type=%s", type(exc).__name__)
        alert()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
