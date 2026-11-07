#!/usr/bin/env python3
"""Report and clean the bot's encrypted database. Runs inside the bot container.

  python scripts/db_maintenance.py report /app/data/bot.db
  python scripts/db_maintenance.py clean /app/data/bot.db             # counts only
  python scripts/db_maintenance.py clean /app/data/bot.db --apply     # after a backup

Only technical rows are removed (db/maintenance.py). --apply writes an encrypted
copy next to the database first and stops if that copy does not pass its check.
The key is read from the Docker secret and is never printed.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_tool import backup, integrity, load_key, open_encrypted
from sqlcipher3 import dbapi2 as sqlcipher

from db.maintenance import clean_apply, clean_counts, report

USAGE = "usage: db_maintenance.py report <db> | clean <db> [--apply]"


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[1] not in ("report", "clean"):
        sys.exit(USAGE)
    command, path, flags = argv[1], argv[2], argv[3:]
    if flags not in ([], ["--apply"]) or (command == "report" and flags):
        sys.exit(USAGE)
    key = load_key()
    now = int(time.time())

    if command == "report":
        conn = open_encrypted(path, key)
        try:
            for label, value in report(conn, now).items():
                print(f"{label}: {value}")
        finally:
            conn.close()
        return

    if not flags:
        conn = open_encrypted(path, key)
        try:
            for label, value in clean_counts(conn, now).items():
                print(f"would delete {label}: {value}")
        finally:
            conn.close()
        print("DRY RUN: nothing deleted. Add --apply to delete these rows.")
        return

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    copy = str(Path(path).with_name(f"before-clean-{stamp}.db"))
    backup(path, copy, key)  # exits if the copy does not pass its check
    conn = open_encrypted(path, key)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        for label, value in clean_apply(conn, now).items():
            print(f"deleted {label}: {value}")
        try:
            conn.execute("VACUUM")  # gives the space back; needs no open transaction
        except sqlcipher.DatabaseError as exc:
            print(f"VACUUM skipped: {type(exc).__name__}; the rows are already deleted")
        result = integrity(conn)
    finally:
        conn.close()
    if result != "ok":
        sys.exit(f"integrity_check failed: {result}. Restore from {copy} if needed.")
    print(f"CLEAN_OK backup kept at {copy}")


if __name__ == "__main__":
    main(sys.argv)
