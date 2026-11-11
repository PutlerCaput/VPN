#!/usr/bin/env python3
"""Move old activity records out of the live database into small sealed archives.

The owner keeps activity records for five years. Kept in the live database they
would grow without bound; here each closed month of each table becomes one file:

    JSON Lines -> xz -> age, encrypted to the owner's PUBLIC key

The private key is not on the server, so nobody who gets into the server can
read five years of records: only the owner can open an archive, on their own
device. A row leaves the database only after its archive was written to disk
and decrypted back to the identical data. The server cannot decrypt for the
owner, so each archive is also sealed to a one-time key that exists for the
check and is destroyed right after it.

    retention_archiver.py --database bot.db --db-key-file db_key \
        --archive-dir /var/lib/vpn-db/archive --recipient age1...
    retention_archiver.py ... --report     what would be archived; changes nothing
    retention_archiver.py --archive-dir DIR --status   one line: OK or WARN

Archives older than --keep-years (5) are deleted. AUTOBAN evidence is left to
scripts/compliance_archiver.py, which has its own rules.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import lzma
import os
import re
import shutil
import sqlite3
import subprocess  # nosec B404
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHUNK = 256 * 1024
# Preset 9 would ask for about 670 MB to compress, more than the server has.
# Preset 6 needs under 100 MB, and a month of records is far smaller than its
# 8 MB dictionary, so nothing is lost.
XZ_PRESET = 6 | lzma.PRESET_EXTREME
RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{58}$")
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
NAME_RE = re.compile(r"^([a-z_]+)-(\d{4}-\d{2})-(\d+)\.jsonl\.xz\.age$")
STALE_AFTER_DAYS = 40


@dataclass(frozen=True)
class Table:
    name: str
    key: str  # integer primary key: rows are exported and deleted in its order
    stamp: str  # unix time the row belongs to
    keep: str  # rows this archiver must not touch ("" = none)


TABLES = (
    Table("conn_logs", "id", "connected_at", ""),
    Table("anomalies", "id", "created_at", "kind = 'AUTOBAN'"),
    Table("audit_log", "id", "created_at", "action = 'autoban'"),
    Table("support_messages", "admin_message_id", "created_at", ""),
)


class ArchiveError(RuntimeError):
    """Stops the run; the message names the step, never a record."""


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def first_of_month(at: datetime, back: int = 0) -> datetime:
    """Midnight UTC on the first day of the month ``back`` months before ``at``."""
    index = at.year * 12 + (at.month - 1) - back
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def connect(path: Path, key: str | None) -> Any:
    if not path.is_file() or path.stat().st_size == 0:
        raise ArchiveError("database_not_found")
    module: Any = sqlite3
    if key:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ArchiveError("invalid_database_key")
        from sqlcipher3 import dbapi2 as sqlcipher

        module = sqlcipher
    connection = module.connect(str(path), timeout=30)
    try:
        if key:
            connection.execute(f"PRAGMA key = \"x'{key}'\"")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        connection.commit()
    except module.DatabaseError:
        connection.close()
        raise ArchiveError("database_key_does_not_open_the_file") from None
    return connection


def where(table: Table) -> str:
    return f"NOT ({table.keep})" if table.keep else "1 = 1"


def months_to_archive(conn: Any, table: Table, cutoff: int) -> list[tuple[str, int]]:
    """Closed months that still have rows in the live database, oldest first."""
    rows = conn.execute(
        f"SELECT strftime('%Y-%m', {table.stamp}, 'unixepoch') AS month, COUNT(*) "  # noqa: S608  # nosec B608 -- names from TABLES
        f"FROM {table.name} WHERE {table.stamp} < ? AND {where(table)} "
        "GROUP BY month ORDER BY month",
        (cutoff,),
    ).fetchall()
    return [(str(month), int(count)) for month, count in rows if month]


def month_bounds(month: str) -> tuple[int, int]:
    match = MONTH_RE.fullmatch(month)
    if match is None:
        raise ArchiveError("unexpected_month")
    start = datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=UTC)
    end = first_of_month(start, back=-1)
    return int(start.timestamp()), int(end.timestamp())


def export_month(
    conn: Any, table: Table, month: str, target: Path
) -> tuple[int, int, int, str]:
    """Writes the month as xz-compressed JSON Lines.

    Returns the row count, the first and last key and the SHA256 of the
    uncompressed JSON Lines.
    """
    start, end = month_bounds(month)
    cursor = conn.execute(
        f"SELECT * FROM {table.name} WHERE {table.stamp} >= ? AND {table.stamp} < ? "  # noqa: S608  # nosec B608 -- names from TABLES
        f"AND {where(table)} ORDER BY {table.key}",
        (start, end),
    )
    columns = [item[0] for item in cursor.description]
    key_at = columns.index(table.key)
    digest = hashlib.sha256()
    count = 0
    first = last = 0
    with lzma.open(target, "wb", preset=XZ_PRESET) as out:
        for row in cursor:
            line = (
                json.dumps(
                    dict(zip(columns, row, strict=True)),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            digest.update(line)
            out.write(line)
            if count == 0:
                first = int(row[key_at])
            last = int(row[key_at])
            count += 1
    with target.open("rb") as written:
        os.fsync(written.fileno())
    return count, first, last, digest.hexdigest()


def tool(name: str) -> str:
    """The full path of age or age-keygen: nothing is started by bare name."""
    found = shutil.which(name)
    if found is None:
        raise ArchiveError("age_is_not_installed")
    return found


def run_tool(arguments: list[str], *, step: str) -> bytes:
    try:
        done = subprocess.run(  # noqa: S603  # nosec B603 -- fixed program, no shell
            arguments, capture_output=True, timeout=600, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ArchiveError(f"{step}_could_not_run") from None
    if done.returncode != 0:
        raise ArchiveError(f"{step}_failed")
    return done.stdout


def one_time_key(folder: Path) -> tuple[str, Path]:
    """A key pair for one check. The identity file is the caller's to destroy."""
    identity = folder / "check.key"
    text = run_tool([tool("age-keygen")], step="one_time_key").decode()
    public = next(
        (line for line in text.split() if RECIPIENT_RE.fullmatch(line)),
        "",
    )
    if not public:
        raise ArchiveError("one_time_key_failed")
    descriptor = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as out:
        out.write(text)
    return public, identity


def decrypted_digest(archive: Path, identity: Path) -> tuple[str, int]:
    """Opens the archive with the one-time key: SHA256 and rows of what is inside."""
    try:
        process = subprocess.Popen(  # noqa: S603  # nosec B603 -- fixed program, no shell
            [tool("age"), "-d", "-i", str(identity), str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise ArchiveError("verify_could_not_run") from None
    digest = hashlib.sha256()
    rows = 0
    unpack = lzma.LZMADecompressor()
    stream = process.stdout
    if stream is None:  # PIPE was requested; this keeps the type checker honest
        process.kill()
        raise ArchiveError("verify_could_not_run")
    try:
        while chunk := stream.read(CHUNK):
            plain = unpack.decompress(chunk)
            digest.update(plain)
            rows += plain.count(b"\n")
    except lzma.LZMAError:
        process.kill()
        raise ArchiveError("verify_failed") from None
    finally:
        process.wait()
    if process.returncode != 0 or not unpack.eof:
        raise ArchiveError("verify_failed")
    return digest.hexdigest(), rows


def fsync_folder(folder: Path) -> None:
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def next_name(folder: Path, table: Table, month: str) -> Path:
    """A month can be archived more than once (late rows): never overwrite."""
    taken = [
        int(match.group(3))
        for path in folder.glob(f"{table.name}-{month}-*.jsonl.xz.age")
        if (match := NAME_RE.fullmatch(path.name))
    ]
    return folder / f"{table.name}-{month}-{max(taken, default=0) + 1}.jsonl.xz.age"


def archive_month(
    conn: Any,
    table: Table,
    month: str,
    archive_dir: Path,
    work: Path,
    recipient: str,
) -> tuple[int, int]:
    """One month of one table: export, seal, check, then delete. Returns rows, bytes."""
    packed = work / "month.jsonl.xz"
    sealed = work / "month.age"
    count, first, last, plain_sha = export_month(conn, table, month, packed)
    if count == 0:
        return 0, 0
    public, identity = one_time_key(work)
    try:
        run_tool(
            [
                tool("age"),
                "-r",
                recipient,
                "-r",
                public,
                "-o",
                str(sealed),
                str(packed),
            ],
            step="encrypt",
        )
        with sealed.open("rb") as written:
            os.fsync(written.fileno())
        folder = archive_dir / table.name
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        final = next_name(folder, table, month)
        shutil.copyfile(sealed, final.with_suffix(".part"))
        with final.with_suffix(".part").open("rb") as copied:
            os.fsync(copied.fileno())
        final.with_suffix(".part").replace(final)
        fsync_folder(folder)
        # What is on disk now, opened the way the owner will open it.
        try:
            seen_sha, seen_rows = decrypted_digest(final, identity)
            if seen_sha != plain_sha or seen_rows != count:
                raise ArchiveError("archive_does_not_match_the_rows")
        except BaseException:
            # An archive that failed its check must not look like a good one.
            final.unlink(missing_ok=True)
            raise
    finally:
        identity.unlink(missing_ok=True)
    manifest = {
        "table": table.name,
        "month": month,
        "rows": count,
        "first_key": first,
        "last_key": last,
        "jsonl_sha256": plain_sha,
        "archive_sha256": file_digest(final),
        "archive_bytes": final.stat().st_size,
        "created_at": int(time.time()),
        "recipient": recipient,
        "format": "jsonl -> xz -> age",
    }
    note = final.with_name(final.name + ".json")
    note.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    with note.open("rb") as written:
        os.fsync(written.fileno())
    fsync_folder(folder)

    start, end = month_bounds(month)
    try:
        removed = conn.execute(
            f"DELETE FROM {table.name} WHERE {table.key} BETWEEN ? AND ? "  # noqa: S608  # nosec B608 -- names from TABLES
            f"AND {table.stamp} >= ? AND {table.stamp} < ? AND {where(table)}",
            (first, last, start, end),
        ).rowcount
        if removed != count:
            # The rows changed between the export and now: keep them all, and
            # the archive too (it is a true copy of what was there).
            conn.rollback()
            raise ArchiveError("rows_changed_during_the_run")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return count, final.stat().st_size


def expired_archives(archive_dir: Path, keep_years: int, now: datetime) -> list[Path]:
    oldest_kept = first_of_month(now, back=keep_years * 12).strftime("%Y-%m")
    expired = []
    for path in sorted(archive_dir.glob("*/*.jsonl.xz.age")):
        match = NAME_RE.fullmatch(path.name)
        if match and match.group(2) < oldest_kept:
            expired.append(path)
    return expired


@contextlib.contextmanager
def single_run(archive_dir: Path) -> Iterator[None]:
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (archive_dir / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ArchiveError("another_run_is_in_progress") from None
        yield


def status(archive_dir: Path) -> int:
    marker = archive_dir / ".last-run"
    if not marker.is_file():
        say("WARN retention archive: has never run")
        return 0
    age_days = int((time.time() - marker.stat().st_mtime) // 86400)
    files = len(list(archive_dir.glob("*/*.jsonl.xz.age")))
    if age_days > STALE_AFTER_DAYS:
        say(f"WARN retention archive: last run {age_days} days ago")
    else:
        say(f"OK retention archive: last run {age_days} days ago, {files} archives")
    return 0


def run(arguments: argparse.Namespace) -> int:
    archive_dir = Path(arguments.archive_dir)
    if arguments.status:
        return status(archive_dir)
    recipient = arguments.recipient or os.environ.get("AGE_RECIPIENT", "")
    if not RECIPIENT_RE.fullmatch(recipient):
        raise ArchiveError("recipient_is_not_an_age_public_key")
    for name in ("age", "age-keygen"):
        tool(name)
    if not 1 <= arguments.hot_months <= 24 or not 1 <= arguments.keep_years <= 50:
        raise ArchiveError("hot_months_or_keep_years_out_of_range")
    key = None
    if arguments.db_key_file:
        key = Path(arguments.db_key_file).read_text().strip() or None
    now = datetime.fromtimestamp(arguments.now or time.time(), tz=UTC)
    cutoff = int(first_of_month(now, back=arguments.hot_months - 1).timestamp())

    with single_run(archive_dir):
        work = archive_dir / ".work"
        # A leftover one-time key would open one archive: nothing may survive a crash.
        shutil.rmtree(work, ignore_errors=True)
        conn = connect(Path(arguments.database), key)
        try:
            plan = [
                (table, month, rows)
                for table in TABLES
                for month, rows in months_to_archive(conn, table, cutoff)
            ]
            expired = expired_archives(archive_dir, arguments.keep_years, now)
            if arguments.report:
                for table, month, rows in plan:
                    say(f"would archive: {table.name} {month} {rows} rows")
                years = arguments.keep_years
                for path in expired:
                    say(f"would delete (older than {years} years): {path.name}")
                say(f"RETENTION_REPORT_DONE months={len(plan)} expired={len(expired)}")
                return 0
            total_rows = total_bytes = 0
            for table, month, _rows in plan:
                work.mkdir(mode=0o700)
                try:
                    rows, size = archive_month(
                        conn, table, month, archive_dir, work, recipient
                    )
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                total_rows += rows
                total_bytes += size
                say(f"RETENTION_ARCHIVED {table.name} {month} rows={rows} bytes={size}")
            for path in expired:
                path.unlink(missing_ok=True)
                path.with_name(path.name + ".json").unlink(missing_ok=True)
                say(f"RETENTION_EXPIRED {path.name}")
        finally:
            conn.close()
        (archive_dir / ".last-run").write_text(f"{int(time.time())}\n")
    say(
        f"RETENTION_DONE months={len(plan)} rows={total_rows} bytes={total_bytes} "
        f"expired={len(expired)}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", default="")
    parser.add_argument("--db-key-file", default="")
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--recipient", default="")
    parser.add_argument("--hot-months", type=int, default=2)
    parser.add_argument("--keep-years", type=int, default=5)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--now", type=int, default=0, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    os.umask(0o077)
    try:
        return run(arguments)
    except ArchiveError as error:
        sys.stderr.write(f"RETENTION_STOPPED: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
