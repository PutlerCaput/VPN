"""scripts/retention_archiver.py: five years of records in small sealed files.

Old months leave the live database only as archives the owner alone can open,
and only after an archive was read back and matched the rows it replaces.
"""

from __future__ import annotations

import json
import lzma
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from db.database import Database

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "retention_archiver.py"
KEY = "ab" * 32
NOW = int(datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp())

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age is not installed",
)


def at(month: int, day: int = 10, year: int = 2026) -> int:
    return int(datetime(year, month, day, 8, tzinfo=UTC).timestamp())


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.database = tmp / "bot.db"
        self.key_file = tmp / "db_key"
        self.key_file.write_text(KEY + "\n")
        self.archive = tmp / "archive"
        self.identity = tmp / "owner.key"
        made = subprocess.run(  # noqa: S603
            ["age-keygen", "-o", str(self.identity)],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        self.recipient = (made.stdout + made.stderr).split("Public key:")[1].split()[0]

    async def create(self) -> None:
        database = Database(str(self.database), KEY)
        await database.connect()
        rows = [
            # (telegram_id, node, connected_at): two old months, two recent ones
            (1001, "u1001", at(8, 3)),
            (1002, "u1002", at(8, 20)),
            (1001, "u1001", at(9, 5)),
            (1001, "u1001", at(10, 7)),
            (1003, "u1003", at(11, 2)),
        ]
        for telegram_id, node, connected in rows:
            await database._execute(
                "INSERT INTO conn_logs (telegram_id, node_username, connected_at, "
                "disconnected_at, bytes_up, bytes_down) VALUES (?, ?, ?, ?, 10, 2000)",
                (telegram_id, node, connected, connected + 600),
            )
        for kind, created in (("TRAFFIC", at(8)), ("AUTOBAN", at(8)), ("NOTE", at(11))):
            await database._execute(
                "INSERT INTO anomalies (telegram_id, node_username, kind, detail, "
                "created_at) VALUES (1001, 'u1001', ?, 'подробности', ?)",
                (kind, created),
            )
        for action, created in (("refund", at(9)), ("autoban", at(9))):
            await database._execute(
                "INSERT INTO audit_log (telegram_id, action, target, detail, "
                "created_at) VALUES (1, ?, '1001', '{}', ?)",
                (action, created),
            )
        await database._execute(
            "INSERT INTO support_messages (admin_message_id, telegram_id, created_at) "
            "VALUES (501, 1001, ?)",
            (at(8),),
        )
        await database.close()

    def run(self, *extra: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(SCRIPT),
                "--database",
                str(self.database),
                "--db-key-file",
                str(self.key_file),
                "--archive-dir",
                str(self.archive),
                "--recipient",
                self.recipient,
                "--now",
                str(NOW),
                *extra,
            ],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        from sqlcipher3 import dbapi2 as sqlcipher

        conn = sqlcipher.connect(str(self.database))
        conn.execute(f"PRAGMA key = \"x'{KEY}'\"")
        try:
            return list(conn.execute(sql).fetchall())
        finally:
            conn.close()

    def files(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.archive))
            for path in self.archive.glob("*/*.jsonl.xz.age")
        )

    def open_as_owner(self, relative: str) -> list[dict[str, Any]]:
        sealed = subprocess.run(  # noqa: S603
            ["age", "-d", "-i", str(self.identity), str(self.archive / relative)],  # noqa: S607
            capture_output=True,
            check=True,
        ).stdout
        return [json.loads(line) for line in lzma.decompress(sealed).splitlines()]


@pytest.fixture
async def box(tmp_path: Path) -> Box:
    made = Box(tmp_path)
    await made.create()
    return made


def test_report_names_the_closed_months_and_changes_nothing(box: Box) -> None:
    result = box.run("--report")
    assert result.returncode == 0, result.stderr
    assert "would archive: conn_logs 2026-08 2 rows" in result.stdout
    assert "would archive: conn_logs 2026-09 1 rows" in result.stdout
    assert "2026-10" not in result.stdout
    assert "2026-11" not in result.stdout
    assert result.stdout.rstrip().endswith("RETENTION_REPORT_DONE months=5 expired=0")
    assert box.files() == []
    assert box.query("SELECT COUNT(*) FROM conn_logs") == [(5,)]


def test_old_months_become_archives_only_the_owner_can_open(box: Box) -> None:
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert "RETENTION_DONE months=5 rows=6" in result.stdout
    assert box.files() == [
        "anomalies/anomalies-2026-08-1.jsonl.xz.age",
        "audit_log/audit_log-2026-09-1.jsonl.xz.age",
        "conn_logs/conn_logs-2026-08-1.jsonl.xz.age",
        "conn_logs/conn_logs-2026-09-1.jsonl.xz.age",
        "support_messages/support_messages-2026-08-1.jsonl.xz.age",
    ]
    # The live database keeps the current and the previous month...
    left = box.query("SELECT connected_at FROM conn_logs ORDER BY id")
    assert left == [(at(10, 7),), (at(11, 2),)]
    # ...and the evidence another archiver is responsible for.
    assert box.query("SELECT kind FROM anomalies ORDER BY id") == [
        ("AUTOBAN",),
        ("NOTE",),
    ]
    assert box.query("SELECT action FROM audit_log") == [("autoban",)]
    assert box.query("SELECT COUNT(*) FROM support_messages") == [(0,)]

    august = box.open_as_owner("conn_logs/conn_logs-2026-08-1.jsonl.xz.age")
    assert [(row["telegram_id"], row["connected_at"]) for row in august] == [
        (1001, at(8, 3)),
        (1002, at(8, 20)),
    ]
    assert august[0]["bytes_down"] == 2000
    anomaly = box.open_as_owner("anomalies/anomalies-2026-08-1.jsonl.xz.age")
    assert anomaly[0]["detail"] == "подробности"

    # The server holds no key that opens an archive, and no readable rows.
    assert not (box.archive / ".work").exists()
    assert list(box.archive.rglob("check.key")) == []
    raw = (box.archive / "conn_logs" / "conn_logs-2026-08-1.jsonl.xz.age").read_bytes()
    assert raw.startswith(b"age-encryption.org/v1")
    assert b"telegram_id" not in raw
    assert b"1001" not in raw

    note = json.loads(
        (
            box.archive / "conn_logs" / "conn_logs-2026-08-1.jsonl.xz.age.json"
        ).read_text()
    )
    assert note["rows"] == 2
    assert note["month"] == "2026-08"
    assert note["recipient"] == box.recipient
    assert note["archive_bytes"] == len(raw)
    for path in box.archive.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0, path


def test_a_second_run_has_nothing_to_do_and_late_rows_get_their_own_file(
    box: Box,
) -> None:
    assert box.run().returncode == 0
    again = box.run()
    assert again.returncode == 0
    assert "RETENTION_DONE months=0 rows=0" in again.stdout
    from sqlcipher3 import dbapi2 as sqlcipher

    conn = sqlcipher.connect(str(box.database))
    conn.execute(f"PRAGMA key = \"x'{KEY}'\"")
    conn.execute(
        "INSERT INTO conn_logs (telegram_id, node_username, connected_at) "
        "VALUES (1009, 'u1009', ?)",
        (at(8, 28),),
    )
    conn.commit()
    conn.close()
    assert box.run().returncode == 0
    assert "conn_logs/conn_logs-2026-08-2.jsonl.xz.age" in box.files()
    first = box.open_as_owner("conn_logs/conn_logs-2026-08-1.jsonl.xz.age")
    late = box.open_as_owner("conn_logs/conn_logs-2026-08-2.jsonl.xz.age")
    assert len(first) == 2
    assert [row["telegram_id"] for row in late] == [1009]


def fake_tool(folder: Path, name: str, body: str) -> dict[str, str]:
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(body)
    path.chmod(0o755)
    return {"PATH": f"{folder}:{os.environ['PATH']}"}


def test_rows_stay_when_the_archive_cannot_be_made(box: Box) -> None:
    env = fake_tool(box.tmp / "bin", "age", "#!/bin/bash\nexit 1\n")
    result = box.run(**env)
    assert result.returncode == 1
    assert "RETENTION_STOPPED: encrypt_failed" in result.stderr
    assert box.files() == []
    assert box.query("SELECT COUNT(*) FROM conn_logs") == [(5,)]
    assert not (box.archive / ".work").exists()


def test_rows_stay_when_the_archive_does_not_read_back(box: Box) -> None:
    # Encryption "succeeds" but writes something else: the check must notice.
    real = shutil.which("age")
    body = f"""#!/bin/bash
if [[ "$1" == "-d" ]]; then exec {real} "$@"; fi
out=""
while [[ $# -gt 0 ]]; do [[ "$1" == "-o" ]] && out="$2"; shift; done
echo "age-encryption.org/v1 but not really" > "$out"
"""
    env = fake_tool(box.tmp / "bin", "age", body)
    result = box.run(**env)
    assert result.returncode == 1
    assert "RETENTION_STOPPED: verify_failed" in result.stderr
    assert box.files() == []
    assert box.query("SELECT COUNT(*) FROM conn_logs") == [(5,)]
    assert list(box.archive.rglob("check.key")) == []


def test_archives_older_than_five_years_are_deleted(box: Box) -> None:
    folder = box.archive / "conn_logs"
    folder.mkdir(parents=True)
    for month in ("2021-10", "2021-11", "2024-01"):
        (folder / f"conn_logs-{month}-1.jsonl.xz.age").write_text("sealed")
        (folder / f"conn_logs-{month}-1.jsonl.xz.age.json").write_text("{}")
    (folder / "notes.txt").write_text("not an archive")
    report = box.run("--report")
    assert "would delete (older than 5 years): conn_logs-2021-10-1" in report.stdout
    assert (folder / "conn_logs-2021-10-1.jsonl.xz.age").exists()
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert "RETENTION_EXPIRED conn_logs-2021-10-1.jsonl.xz.age" in result.stdout
    # November 2021 is exactly five years back from November 2026: kept.
    kept = sorted(path.name for path in folder.glob("conn_logs-202[14]-*"))
    assert kept == [
        "conn_logs-2021-11-1.jsonl.xz.age",
        "conn_logs-2021-11-1.jsonl.xz.age.json",
        "conn_logs-2024-01-1.jsonl.xz.age",
        "conn_logs-2024-01-1.jsonl.xz.age.json",
    ]
    assert (folder / "notes.txt").exists()


def test_the_hot_window_is_a_setting(box: Box) -> None:
    result = box.run("--hot-months", "1", "--report")
    assert "would archive: conn_logs 2026-10 1 rows" in result.stdout
    assert "2026-11" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (["--recipient", "age1short"], "recipient_is_not_an_age_public_key"),
        (["--database", "/nonexistent/bot.db"], "database_not_found"),
        (["--hot-months", "0"], "hot_months_or_keep_years_out_of_range"),
        (["--keep-years", "0"], "hot_months_or_keep_years_out_of_range"),
    ],
)
def test_bad_settings_stop_before_anything_happens(
    box: Box, arguments: list[str], reason: str
) -> None:
    result = box.run(*arguments)
    assert result.returncode == 1
    assert f"RETENTION_STOPPED: {reason}" in result.stderr
    assert box.files() == []


def test_a_wrong_database_key_stops_the_run(box: Box) -> None:
    box.key_file.write_text("cd" * 32)
    result = box.run()
    assert result.returncode == 1
    assert "database_key_does_not_open_the_file" in result.stderr


def test_a_plain_database_works_too(tmp_path: Path) -> None:
    box = Box(tmp_path)
    with sqlite3.connect(box.database) as conn:
        conn.executescript(
            """
            CREATE TABLE conn_logs (
                id INTEGER PRIMARY KEY, telegram_id INTEGER, connected_at INTEGER);
            CREATE TABLE anomalies (
                id INTEGER PRIMARY KEY, kind TEXT, created_at INTEGER);
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY, action TEXT, created_at INTEGER);
            CREATE TABLE support_messages (
                admin_message_id INTEGER PRIMARY KEY, created_at INTEGER);
            """
        )
        conn.execute("INSERT INTO conn_logs VALUES (1, 7, ?)", (at(7),))
    box.key_file.write_text("")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.files() == ["conn_logs/conn_logs-2026-07-1.jsonl.xz.age"]


def test_status_line(box: Box) -> None:
    def status() -> str:
        return subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(SCRIPT),
                "--archive-dir",
                str(box.archive),
                "--status",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    assert status().startswith("WARN retention archive: has never run")
    assert box.run().returncode == 0
    assert status().startswith("OK retention archive: last run 0 days ago, 5 archives")
    old = time.time() - 50 * 86400
    os.utime(box.archive / ".last-run", (old, old))
    assert status().startswith("WARN retention archive: last run 50 days ago")
