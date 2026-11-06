"""SQLCipher maintenance: encrypt an existing database, take an online backup, check it.

Runs inside the bot container, where the key is mounted as a Docker secret:
  python scripts/db_tool.py encrypt /app/data/bot.db
  python scripts/db_tool.py backup /app/data/bot.db /app/data/backup.db
  python scripts/db_tool.py check /app/data/bot.db
(prefix each with `docker compose exec -T bot`).

The key is read from /run/secrets/db_key or the DB_KEY variable and is never printed.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from sqlcipher3 import dbapi2 as sqlcipher

HEX_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SECRET_FILE = Path(os.environ.get("SECRETS_DIR", "/run/secrets")) / "db_key"


def load_key() -> str:
    key = SECRET_FILE.read_text().strip() if SECRET_FILE.is_file() else ""
    key = key or os.environ.get("DB_KEY", "").strip()
    if not HEX_KEY_RE.fullmatch(key):
        sys.exit("db_key is missing or is not 64 hex characters")
    return key


def open_encrypted(path: str, key: str) -> Any:
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


def main(argv: list[str]) -> None:
    usage = "usage: db_tool.py encrypt <db> | backup <src> <dst> | check <db>"
    if len(argv) < 2:
        sys.exit(usage)
    command, args = argv[1], argv[2:]
    if command == "encrypt" and len(args) == 1:
        encrypt(args[0], load_key())
    elif command == "backup" and len(args) == 2:
        backup(args[0], args[1], load_key())
    elif command == "check" and len(args) == 1:
        check(args[0], load_key())
    else:
        sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv)
