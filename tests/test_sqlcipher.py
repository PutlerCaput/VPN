"""Database encryption at rest (SQLCipher) and the maintenance tool."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from db.database import Database, DatabaseKeyError

KEY = "ab" * 32
OTHER_KEY = "cd" * 32
ROOT = Path(__file__).resolve().parent.parent


async def test_encrypted_roundtrip_and_wrong_key(tmp_path: Path) -> None:
    path = str(tmp_path / "bot.db")
    db = Database(path, KEY)
    await db.connect()
    await db.create_user(111, "alice", None)
    await db.set_receipt_email(111, "alice@example.com")
    await db.close()

    raw = Path(path).read_bytes()
    assert not raw.startswith(b"SQLite format 3")
    assert b"alice@example.com" not in raw
    plain = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError):
        plain.execute("SELECT * FROM users").fetchall()
    plain.close()

    reopened = Database(path, KEY)
    await reopened.connect()
    user = await reopened.get_user(111)
    await reopened.close()
    assert user is not None
    assert user["receipt_email"] == "alice@example.com"

    with pytest.raises(DatabaseKeyError):
        await Database(path, OTHER_KEY).connect()


def test_key_format_is_enforced() -> None:
    with pytest.raises(DatabaseKeyError):
        Database(":memory:", "x'; ATTACH DATABASE 'evil' AS e; --")


async def test_migration_adds_receipt_email(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT, "
        "first_name TEXT, status TEXT NOT NULL DEFAULT 'pending', expires_at INTEGER, "
        "node_username TEXT UNIQUE, referred_by INTEGER, config_link TEXT, "
        "trial_used INTEGER NOT NULL DEFAULT 0, expiry_reminded_for INTEGER, "
        "created_at INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()
    db = Database(str(path))
    await db.connect()
    await db.create_user(222, "bob", None)
    await db.set_receipt_email(222, "bob@example.com")
    assert (await db.get_user(222))["receipt_email"] == "bob@example.com"
    await db.close()


def _tool(*args: str, key: str = KEY) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "db_tool.py"), *args],
        env={"DB_KEY": key, "SECRETS_DIR": "/nonexistent", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


async def test_db_tool_encrypts_plain_database_and_backs_up(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    plain = Database(str(path))
    await plain.connect()
    await plain.create_user(333, "carol", None)
    await plain.close()

    result = _tool("encrypt", str(path))
    assert result.returncode == 0, result.stderr
    assert KEY not in result.stdout + result.stderr
    assert (tmp_path / "bot.db.plain-backup").exists()

    encrypted = Database(str(path), KEY)
    await encrypted.connect()
    assert (await encrypted.get_user(333)) is not None
    await encrypted.close()

    backup = tmp_path / "backup.db"
    assert _tool("backup", str(path), str(backup)).returncode == 0
    assert _tool("check", str(backup)).returncode == 0
    assert _tool("check", str(backup), key=OTHER_KEY).returncode != 0
    assert _tool("encrypt", str(path)).returncode != 0  # already encrypted
    assert _tool("check", str(backup), key="short").returncode != 0
