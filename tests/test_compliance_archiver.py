from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from scripts import compliance_archiver as archiver

KEY = "high-entropy-test-key-" * 4
NOW = 1800000000
CUTOFF = NOW - archiver.RETENTION_SECONDS
SCHEMA = """
CREATE TABLE anomalies (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER,
node_username TEXT, kind TEXT NOT NULL, detail TEXT, created_at INTEGER NOT NULL);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER,
action TEXT NOT NULL, target TEXT, detail TEXT, created_at INTEGER NOT NULL);
"""


def seed(connection: Any) -> None:
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO anomalies VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 10, "node10", "AUTOBAN", '{"reason":"test"}', CUTOFF - 1),
            (2, 20, "node20", "AUTOBAN", "at boundary", CUTOFF),
            (3, 30, "node30", "AUTOBAN", "recent", NOW),
            (4, 40, "node40", "REVIEW", "not confirmed", CUTOFF - 100),
        ],
    )
    connection.execute(
        "INSERT INTO audit_log VALUES (1, 10, 'autoban', NULL, NULL, ?)",
        (CUTOFF - 1,),
    )
    connection.execute(
        "INSERT INTO audit_log VALUES (2, 20, 'login', NULL, NULL, ?)",
        (CUTOFF - 100,),
    )
    connection.commit()


@pytest.fixture
def environment(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "bot.db"
    with sqlite3.connect(database) as connection:
        seed(connection)
    cold = tmp_path / "cold"
    cold.mkdir(mode=0o700)
    return database, cold


def contents(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = subprocess.run(  # noqa: S603 -- fixed GPG read-only test
        [
            shutil.which("gpg") or "/usr/bin/gpg",
            "--batch",
            "--no-tty",
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--decrypt",
            str(path),
        ],
        input=(KEY + "\n").encode(),
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
        data = archive.extractfile("evidence.jsonl")
        manifest = archive.extractfile("manifest.json")
        assert data is not None
        assert manifest is not None
        return [json.loads(line) for line in data], json.load(manifest)


def test_real_encrypt_verify_and_purge_only_eligible_rows(
    environment: tuple[Path, Path],
) -> None:
    database, cold = environment
    result = archiver.run(database, cold, KEY, now=NOW)
    assert result["status"] == "archived"
    assert result["rows"] == result["deleted"] == 2
    archive = Path(result["archive"])
    assert archive.name == "compliance_archive_2027_01_15.tar.gz.enc"
    assert archiver.digest_file(archive) == result["sha256"]
    rows, manifest = contents(archive)
    assert manifest["rows"] == 2
    assert {row["table"] for row in rows} == {"anomalies", "audit_log"}
    assert all(row["row"][0] == 1 for row in rows)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id FROM anomalies ORDER BY id"
        ).fetchall() == [(2,), (3,), (4,)]
        assert connection.execute("SELECT action FROM audit_log").fetchall() == [
            ("login",)
        ]
    assert (archive.parent / "completed.json").is_file()
    assert archiver.run(database, cold, KEY, now=NOW)["status"] == "nothing_to_archive"


def test_corrupt_ciphertext_never_purges(
    environment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, cold = environment
    original = archiver.encrypt

    def corrupt(source: Path, destination: Path, *args: Any) -> None:
        original(source, destination, *args)
        value = bytearray(destination.read_bytes())
        value[-15] ^= 0xFF
        destination.write_bytes(value)

    monkeypatch.setattr(archiver, "encrypt", corrupt)
    with pytest.raises((archiver.ArchiveError, tarfile.ReadError)):
        archiver.run(database, cold, KEY, now=NOW)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == 4
    assert not list(cold.rglob("completed.json"))
    assert not list(cold.rglob("*.tar.gz.enc"))


def test_concurrent_updates_and_inserts_are_not_deleted(
    environment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, cold = environment
    original = archiver.verify_archive

    def change_after_verification(*args: Any) -> None:
        original(*args)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE anomalies SET detail='changed' WHERE id=1")
            connection.execute(
                "INSERT INTO anomalies VALUES "
                "(5, 50, NULL, 'AUTOBAN', 'new late row', ?)",
                (CUTOFF - 1,),
            )

    monkeypatch.setattr(archiver, "verify_archive", change_after_verification)
    result = archiver.run(database, cold, KEY, now=NOW)
    assert result["deleted"] == 1  # Only the unchanged audit row.
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT detail FROM anomalies WHERE id=1"
        ).fetchone() == ("changed",)
        assert connection.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == 5


def test_archive_survives_purge_failure_and_same_day_retry(
    environment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, cold = environment
    original = archiver.purge_rows

    def fail(*args: Any) -> int:
        raise sqlite3.OperationalError("simulated lock")

    monkeypatch.setattr(archiver, "purge_rows", fail)
    with pytest.raises(sqlite3.OperationalError):
        archiver.run(database, cold, KEY, now=NOW)
    first = list(cold.rglob("*.tar.gz.enc"))
    assert len(first) == 1
    assert not list(cold.rglob("completed.json"))
    before = archiver.digest_file(first[0])
    monkeypatch.setattr(archiver, "purge_rows", original)
    result = archiver.run(database, cold, KEY, now=NOW)
    assert result["deleted"] == 2
    assert len(list(cold.rglob("*.tar.gz.enc"))) == 2
    assert archiver.digest_file(first[0]) == before


def test_missing_or_wrong_schema_does_not_create_database(tmp_path: Path) -> None:
    cold = tmp_path / "cold"
    cold.mkdir(mode=0o700)
    database = tmp_path / "missing.db"
    with pytest.raises(archiver.ArchiveError):
        archiver.run(database, cold, KEY, now=NOW)
    assert not database.exists()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(archiver.ArchiveError):
        archiver.run(database, cold, KEY, now=NOW)


def test_sqlcipher_archive_and_wrong_key_fail_closed(tmp_path: Path) -> None:
    from sqlcipher3 import dbapi2 as sqlcipher

    database = tmp_path / "encrypted.db"
    db_key = "ab" * 32
    connection = sqlcipher.connect(database)
    connection.execute(f"PRAGMA key = \"x'{db_key}'\"")
    seed(connection)
    connection.close()
    cold = tmp_path / "cold"
    cold.mkdir(mode=0o700)
    with pytest.raises(sqlcipher.DatabaseError):
        archiver.run(database, cold, KEY, db_key="00" * 32, now=NOW)
    result = archiver.run(database, cold, KEY, db_key=db_key, now=NOW)
    assert result["deleted"] == 2
    connection = sqlcipher.connect(database)
    connection.execute(f"PRAGMA key = \"x'{db_key}'\"")
    assert connection.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == 3
    connection.close()


def test_insecure_secret_file_is_rejected(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text(KEY)
    key.chmod(0o644)
    with pytest.raises(archiver.ArchiveError):
        archiver.private_secret(key, "UNUSED")
    key.chmod(0o600)
    assert archiver.private_secret(key, "UNUSED") == KEY
