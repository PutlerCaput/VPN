"""SQLCipher maintenance: encrypt an existing database, take an online backup, check it.

Runs inside the bot container, where the key is mounted as a Docker secret:
  python scripts/db_tool.py encrypt /app/data/bot.db
  python scripts/db_tool.py backup /app/data/bot.db /app/data/backup.db
  python scripts/db_tool.py check /app/data/bot.db
  python scripts/db_tool.py migrate-check /app/data/bot.db
(prefix each with `docker compose exec -T bot`, or run the new image with
`docker run` before switching to it).

migrate-check copies the database, applies this release's schema changes to
the copy only, compares row counts and integrity, and deletes the copy. The
original file is opened read-only for the copy and never modified.

The key is read from /run/secrets/db_key or the DB_KEY variable and is never printed.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from sqlcipher3 import dbapi2 as sqlcipher

HEX_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SECRET_FILE = Path(os.environ.get("SECRETS_DIR", "/run/secrets")) / "db_key"


def load_key() -> str:
    key = SECRET_FILE.read_text().strip() if SECRET_FILE.is_file() else ""
    key = key or os.environ.get("DB_KEY", "").strip()
    if not HEX_KEY_RE.fullmatch(key):
        sys.exit("db_key is missing or is not 64 hex characters")
    return key


def open_encrypted(path: str, key: str) -> Any:
    # sqlcipher would silently create an empty database for a wrong path.
    if not Path(path).is_file() or Path(path).stat().st_size == 0:
        sys.exit(f"{path}: database file not found or empty")
    conn = sqlcipher.connect(path)
    conn.execute(f"PRAGMA key = \"x'{key}'\"")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher.DatabaseError:
        conn.close()
        sys.exit(f"{path}: wrong key or not an encrypted database")
    conn.commit()  # end the implicit transaction sqlcipher3 opened
    return conn


def integrity(conn: Any) -> str:
    return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def encrypt(path: str, key: str) -> None:
    source = Path(path)
    target = source.with_name(source.name + ".enc")
    plain = sqlcipher.connect(str(source))
    try:
        plain.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher.DatabaseError:
        sys.exit(f"{path}: already encrypted or not a SQLite database")
    plain.commit()
    plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    plain.execute(f"ATTACH DATABASE ? AS encrypted KEY \"x'{key}'\"", (str(target),))
    plain.execute("SELECT sqlcipher_export('encrypted')")
    plain.execute("DETACH DATABASE encrypted")
    plain.close()
    check = open_encrypted(str(target), key)
    result = integrity(check)
    check.close()
    if result != "ok":
        target.unlink()
        sys.exit(f"integrity_check failed on the encrypted copy: {result}")
    source.rename(source.with_name(source.name + ".plain-backup"))
    for suffix in ("-wal", "-shm"):
        Path(str(source) + suffix).unlink(missing_ok=True)
    target.rename(source)
    print(f"encrypted {path}; plaintext copy kept as {source.name}.plain-backup")
    print("verify the bot starts, then shred the .plain-backup file")


def backup(src: str, dst: str, key: str) -> None:
    Path(dst).unlink(missing_ok=True)
    source = open_encrypted(src, key)
    target = sqlcipher.connect(dst)
    target.execute(f"PRAGMA key = \"x'{key}'\"")
    source.backup(target)
    result = integrity(target)
    target.close()
    source.close()
    if result != "ok":
        Path(dst).unlink(missing_ok=True)
        sys.exit(f"integrity_check failed on the backup: {result}")
    print(f"backup written: {dst}")


def check(path: str, key: str) -> None:
    conn = open_encrypted(path, key)
    result = integrity(conn)
    conn.close()
    if result != "ok":
        sys.exit(f"integrity_check failed: {result}")
    print("ok")


def _table_counts(conn: Any) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    counts = {}
    for name in names:
        # Only plain identifiers are interpolated; anything else stops the check.
        if not TABLE_NAME_RE.fullmatch(name):
            sys.exit(f"unexpected table name in {name!r}")
        query = f'SELECT COUNT(*) FROM "{name}"'  # noqa: S608  # nosec B608 -- validated identifier
        counts[name] = int(conn.execute(query).fetchone()[0])
    return counts


def migrate_check(path: str, key: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.database import Database

    copy = Path(path).with_name(Path(path).name + ".migrate-check")
    backup(path, str(copy), key)
    try:
        conn = open_encrypted(str(copy), key)
        before = _table_counts(conn)
        conn.close()

        async def apply() -> None:
            database = Database(str(copy), key)
            await database.connect()
            await database.close()

        asyncio.run(apply())
        conn = open_encrypted(str(copy), key)
        after = _table_counts(conn)
        result = integrity(conn)
        conn.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(copy) + suffix).unlink(missing_ok=True)
    # Migrations may add rows (app_meta markers) but must never drop any.
    lost = {
        name: (count, after.get(name))
        for name, count in before.items()
        if after.get(name, -1) < count
    }
    for name, count in sorted(after.items()):
        marker = "" if name in before else "  (new table)"
        print(f"{name}: {before.get(name, 0)} -> {count}{marker}")
    if lost or result != "ok":
        sys.exit(f"MIGRATION_CHECK_FAILED rows={lost} integrity={result}")
    print("MIGRATION_CHECK_OK")


def main(argv: list[str]) -> None:
    usage = (
        "usage: db_tool.py encrypt <db> | backup <src> <dst> | check <db> | "
        "migrate-check <db>"
    )
    if len(argv) < 2:
        sys.exit(usage)
    command, args = argv[1], argv[2:]
    if command == "encrypt" and len(args) == 1:
        encrypt(args[0], load_key())
    elif command == "backup" and len(args) == 2:
        backup(args[0], args[1], load_key())
    elif command == "check" and len(args) == 1:
        check(args[0], load_key())
    elif command == "migrate-check" and len(args) == 1:
        migrate_check(args[0], load_key())
    else:
        sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv)
