"""Send a file or a short note to the owner in Telegram, as the bot.

Runs inside the bot container, where the token and the owner's id are already
configured, so no secret passes through the shell:

  python scripts/send_backup.py file /app/data/.outgoing-backup.age "caption"
  python scripts/send_backup.py text "message"

Prints SENT on success and the name of the error class (never its text, which
may hold a URL with the token) on failure. Exit codes: 0 sent, 1 not sent,
2 usage, 3 file too big for the Bot API.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")

# The Bot API takes documents up to 50 MB; stay below it.
MAX_BYTES = 45 * 1024 * 1024
USAGE = 2
TOO_BIG = 3


async def deliver(bot: Any, owner: int, kind: str, text: str, path: str) -> int:
    """Send through `bot`. Returns the exit code."""
    if kind == "file":
        from aiogram.types import FSInputFile

        file = Path(path)
        if not file.is_file():
            print("NO_FILE")
            return 1
        if file.stat().st_size > MAX_BYTES:
            print("TOO_BIG")
            return TOO_BIG
        await bot.send_document(owner, FSInputFile(file), caption=text or None)
    else:
        await bot.send_message(owner, text)
    print("SENT")
    return 0


async def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("file", "text"):
        print(__doc__)
        return USAGE
    kind = argv[0]
    path, text = (
        (argv[1], argv[2] if len(argv) > 2 else "") if kind == "file" else ("", argv[1])
    )
    try:
        from aiogram import Bot

        from config import get_settings
        from services.telegram_api import bot_session

        settings = get_settings()
        bot = Bot(
            token=settings.bot_token.get_secret_value(),
            session=bot_session(settings.telegram_proxy_url),
        )
    except Exception as exc:
        print(f"SETTINGS_{type(exc).__name__}")
        return 1
    try:
        return await deliver(bot, settings.admin_telegram_id, kind, text, path)
    except Exception as exc:
        print(f"NOT_SENT_{type(exc).__name__}")
        return 1
    finally:
        await bot.session.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
