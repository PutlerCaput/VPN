"""scripts/migrate_marzban_users.py: export of Marzban users keeps VLESS ids."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

from scripts.migrate_marzban_users import _mask, export_marzban, write_private_json

VLESS_ID = "0b6f6b1e-7a4c-4f53-9b55-2d1a6f0c9e11"


def _marzban_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, status TEXT,
            expire INTEGER, data_limit INTEGER, used_traffic INTEGER);
        CREATE TABLE proxies (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT,
            settings TEXT);
        """
    )
    conn.execute("INSERT INTO users VALUES (1, 'tg_1', 'active', 2000000000, 0, 5)")
    conn.execute("INSERT INTO users VALUES (2, 'tg_2', 'disabled', NULL, NULL, 0)")
    conn.execute(
        "INSERT INTO proxies VALUES (1, 1, 'VLESS', ?)",
        (json.dumps({"id": VLESS_ID.upper(), "flow": "xtls-rprx-vision"}),),
    )
    conn.execute("INSERT INTO proxies VALUES (2, 2, 'VLESS', '{\"id\": \"bad\"}')")
    conn.commit()
    conn.close()


def test_export_reads_vless_ids_read_only(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    _marzban_db(db)
    before = db.stat().st_mtime_ns
    records = {r["username"]: r for r in export_marzban(str(db))}
    assert records["tg_1"]["uuid"] == VLESS_ID
    assert records["tg_1"]["expire"] == 2000000000
    assert records["tg_2"]["uuid"] is None
    assert db.stat().st_mtime_ns == before


def test_map_file_is_private_and_mask_hides_values(tmp_path: Path) -> None:
    out = tmp_path / "map.json"
    write_private_json(str(out), [{"uuid": VLESS_ID}])
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert VLESS_ID not in _mask(VLESS_ID)
