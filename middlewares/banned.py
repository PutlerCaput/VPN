"""Blocks banned users from everything except /start and /legal."""

from __future__ import annotations

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


class BannedMiddleware(BaseMiddleware):
    def __init__(self, db: UserStatusSource) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        record = await self.db.get_user(user.id)
        banned = record is not None and record["status"] == "banned"
        data["db_user"] = record
        if not banned or is_allowed_for_banned(event):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer(BANNED_TEXT)
        elif isinstance(event, CallbackQuery):
            await event.answer(BANNED_TEXT, show_alert=True)
        return None
