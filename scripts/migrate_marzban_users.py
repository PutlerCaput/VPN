"""One-time move of subscribers from Marzban to Hiddify Manager.

Two steps, both dry runs unless ``--apply`` is given:

1. On the server host (stdlib only, the Marzban database is opened read-only)::

     python3 scripts/migrate_marzban_users.py export \\
         --marzban-db /var/lib/marzban/db.sqlite3 --out /root/backups/mz_users.json

   Writes, with mode 0600, one record per Marzban user: username, VLESS id,
   status, expiry and traffic. The VLESS id is a credential: keep the file on the
   server and delete it after step 2.

2. Inside the bot container (needs the bot settings and the Hiddify secret)::

     python -m scripts.migrate_marzban_users import --map /tmp/mz_users.json
     python -m scripts.migrate_marzban_users import --map /tmp/mz_users.json --apply

   For every bot user whose ``node_username`` is a Marzban username, creates a
   Hiddify user with the SAME VLESS id as its UUID (so the id the client already
   knows stays valid), the expiry the bot has on record and the tier's traffic
   policy, then stores that UUID as ``node_username``. Users that are not active
   only get the UUID stored; they are provisioned on their next payment.

Output never contains UUIDs, links or Telegram IDs: only counts and masked ids.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _mask(value: object) -> str:
    text = str(value)
    return text[:2] + "…" + text[-2:] if len(text) > 4 else "…"


def export_marzban(db_path: str) -> list[dict[str, Any]]:
    """Users and their VLESS ids from a Marzban SQLite database (read-only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT u.username, u.status, u.expire, u.data_limit, u.used_traffic, "
            "p.settings FROM users u JOIN proxies p ON p.user_id = u.id "
            "WHERE p.type = 'VLESS'"
        ).fetchall()
    finally:
        conn.close()
    records = []
    for username, status, expire, data_limit, used, settings in rows:
        vless_id = str(json.loads(settings or "{}").get("id") or "").lower()
        records.append(
            {
                "username": username,
                "uuid": vless_id if _is_uuid(vless_id) else None,
                "status": status,
                "expire": int(expire) if expire else None,
                "data_limit": int(data_limit) if data_limit else None,
                "used_traffic": int(used or 0),
            }
        )
    return records


def write_private_json(path: str, data: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


async def import_into_hiddify(map_path: str, apply: bool) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import DEFAULT_TIER, TIERS, get_settings
    from main import open_database
    from services.hiddify import HiddifyClient

    records = json.loads(Path(map_path).read_text(encoding="utf-8"))
    settings = get_settings()
    db = await open_database(settings)
    hiddify = HiddifyClient(
        settings.hiddify_url,
        settings.hiddify_api_key.get_secret_value(),
        settings.hiddify_admin_proxy_path,
        user_proxy_path=settings.hiddify_user_proxy_path,
        subscription_base_url=settings.subscription_base_url,
        verify_tls=settings.hiddify_verify_tls,
        timeout=settings.hiddify_timeout,
    )
    counts = {"created": 0, "stored_only": 0, "no_bot_user": 0, "skipped": 0}
    failures = 0
    try:
        for record in records:
            username = str(record.get("username") or "")
            vless_id = record.get("uuid")
            user = await db.get_user_by_node(username) if username else None
            if user is None:
                counts["no_bot_user"] += 1
                continue
            if not vless_id or not _is_uuid(str(vless_id)):
                counts["skipped"] += 1
                print(f"skip {_mask(username)}: no valid VLESS id")
                continue
            tier = str(user.get("tier") or DEFAULT_TIER)
            tier = tier if tier in TIERS else DEFAULT_TIER
            active = user.get("status") == "active"
            expires = int(user.get("expires_at") or 0)
            action = "create" if active and expires else "store"
            mode = "APPLY" if apply else "plan"
            print(f"{mode} {action} {_mask(username)} tier={tier}")
            if not apply:
                counts["created" if action == "create" else "stored_only"] += 1
                continue
            try:
                if action == "create":
                    await hiddify.create_user(str(vless_id), expire=expires, tier=tier)
                await db.set_node_username(int(user["telegram_id"]), str(vless_id))
            except Exception as exc:  # report and continue with the next user
                failures += 1
                print(f"FAILED {_mask(username)}: {type(exc).__name__}")
                continue
            counts["created" if action == "create" else "stored_only"] += 1
    finally:
        await hiddify.close()
        await db.close()
    print(json.dumps({"apply": apply, **counts, "failed": failures}))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="read Marzban users (host, read-only)")
    exp.add_argument("--marzban-db", required=True)
    exp.add_argument("--out", required=True)
    imp = sub.add_parser("import", help="create Hiddify users (bot container)")
    imp.add_argument("--map", required=True)
    imp.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "export":
        records = export_marzban(args.marzban_db)
        write_private_json(args.out, records)
        valid = sum(1 for r in records if r["uuid"])
        print(json.dumps({"users": len(records), "with_vless_id": valid}))
        return 0
    return asyncio.run(import_into_hiddify(args.map, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
