#!/usr/bin/env python3
"""Entry sharding: which segment of entry nodes a user's subscription shows.

Runs inside the bot container (it has the settings and the database):

    docker exec -i node-rental-bot-1 python3 - [args] < scripts/entry_segments.py

    (no args)                 the plan (segments, shared hosts, strict mode),
                              users per segment, and whether sharding is on
    --show TELEGRAM_ID        the user's segment and where it comes from
    --set TELEGRAM_ID NAME    admin override: pin the user to segment NAME
                              (for example after their entry was blocked)
    --clear TELEGRAM_ID       drop the pin; the next download assigns one again

Nothing secret is printed: segment names, host addresses and counts only;
no UUID, no subscription link. docs/ARCHITECTURE.md, "Control plane".
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

sys.path.insert(0, "/app")


def _usage() -> None:
    print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
    raise SystemExit(2)


async def run(argv: list[str]) -> int:
    from config import Settings
    from main import build_database
    from services.sharding import assign_segment, derive_key, plan_from_settings

    settings = Settings()
    plan = plan_from_settings(settings)
    db: Any = build_database(settings)
    await db.connect()
    try:
        if not argv:
            state = "ON" if settings.entry_sharding_enabled else "off"
            print(f"ENTRY_SHARDING: {state}, strict={plan.strict}")
            for name in plan.names:
                print(f"  segment {name}: {', '.join(sorted(plan.segments[name]))}")
            if plan.shared:
                print(f"  shared: {', '.join(sorted(plan.shared))}")
            for row in await db.entry_segment_counts():
                known = "" if row["segment"] in plan.segments else " (not configured)"
                print(
                    f"  users {row['segment']}/{row['source']}: {row['users']}{known}"
                )
            return 0
        command = argv[0]
        if command not in ("--show", "--set", "--clear") or len(argv) < 2:
            _usage()
        telegram_id = int(argv[1])
        if await db.get_user(telegram_id) is None:
            print(f"ENTRY_SEGMENTS_FAIL: no user {telegram_id}")
            return 1
        if command == "--show":
            row = await db.get_entry_segment(telegram_id)
            if row:
                print(f"user {telegram_id}: {row['segment']} ({row['source']})")
            elif plan.names:
                key = derive_key(settings.jwt_secret.get_secret_value())
                guess = assign_segment(telegram_id, plan.names, key)
                print(f"user {telegram_id}: not pinned yet, would get {guess}")
            else:
                print(f"user {telegram_id}: not pinned, no segments configured")
            return 0
        if command == "--set":
            if len(argv) != 3:
                _usage()
            name = argv[2]
            if name not in plan.segments:
                print(f"ENTRY_SEGMENTS_FAIL: segment {name!r} is not in ENTRY_SEGMENTS")
                return 1
            await db.set_entry_segment(telegram_id, name, "admin")
            print(f"ENTRY_SEGMENTS_OK: user {telegram_id} -> {name} (admin)")
            return 0
        await db.clear_entry_segment(telegram_id)
        print(f"ENTRY_SEGMENTS_OK: user {telegram_id} unpinned")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(sys.argv[1:])))
