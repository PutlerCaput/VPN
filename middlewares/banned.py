"""Blocks banned users from everything except /start and /legal."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

# /terms and /paysupport stay available: payment disputes must be reachable.
ALLOWED_COMMANDS = ("/start", "/legal", "/terms", "/paysupport")
BANNED_TEXT = (
    "🔴 Доступ заблокирован за нарушение правил использования. "
    "Доступны только /start, /legal, /terms и /paysupport."
)


# Every update used to wait for a database round trip to the other server
# (about 40-50 ms) before its handler ran. The answer "not banned" is kept this
# long per user; a ban or unban reaches the menus at most this much later.
STATUS_CACHE_SECONDS = 30.0
STATUS_CACHE_SIZE = 10_000


class UserStatusSource(Protocol):
    async def get_user(self, telegram_id: int) -> dict[str, Any] | None: ...


def is_allowed_for_banned(event: TelegramObject) -> bool:
    if isinstance(event, Message) and (
        getattr(event, "successful_payment", None) is not None
        or getattr(event, "refunded_payment", None) is not None
    ):
        # The payment is recorded so the owner can review or refund it.
        return True
    if isinstance(event, Message) and event.text:
        command = event.text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        return command in ALLOWED_COMMANDS
    return False


def needs_fresh_record(event: TelegramObject) -> bool:
    """/start, /paysupport and payments read the user record (``db_user``)."""
    return is_allowed_for_banned(event)


class BannedMiddleware(BaseMiddleware):
    def __init__(
        self,
        db: UserStatusSource,
        *,
        cache_seconds: float = STATUS_CACHE_SECONDS,
        cache_size: int = STATUS_CACHE_SIZE,
    ) -> None:
        self.db = db
        self.cache_seconds = cache_seconds
        self.cache_size = cache_size
        # telegram_id -> (when checked, banned)
        self._status: OrderedDict[int, tuple[float, bool]] = OrderedDict()

    def _cached_banned(self, telegram_id: int, now: float) -> bool | None:
        entry = self._status.get(telegram_id)
        if entry is None or now - entry[0] >= self.cache_seconds:
            return None
        return entry[1]

    def _remember(self, telegram_id: int, banned: bool, now: float) -> None:
        self._status[telegram_id] = (now, banned)
        self._status.move_to_end(telegram_id)
        while len(self._status) > self.cache_size:
            self._status.popitem(last=False)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        now = time.monotonic()
        banned = (
            None if needs_fresh_record(event) else self._cached_banned(user.id, now)
        )
        if banned is None:
            record = await self.db.get_user(user.id)
            banned = record is not None and record["status"] == "banned"
            # Only a record read just now is handed on: handlers that use it
            # (/start, /paysupport) always come through this branch.
            data["db_user"] = record
            self._remember(user.id, banned, now)
        if not banned or is_allowed_for_banned(event):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer(BANNED_TEXT)
        elif isinstance(event, CallbackQuery):
            await event.answer(BANNED_TEXT, show_alert=True)
        return None
