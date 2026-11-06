"""Per-user sliding-window throttling for bot updates (30 req/min)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from services.security import SlidingWindowLimiter

THROTTLE_TEXT = "⏳ Слишком много запросов. Подождите минуту."


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limiter: SlidingWindowLimiter | None = None) -> None:
        self.limiter = limiter or SlidingWindowLimiter(limit=30, window_seconds=60)
        self._warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if self.limiter.hit(str(user.id)):
            return await handler(event, data)
        now = time.monotonic()
        # Warn at most once per window so throttling itself cannot be abused.
        if now - self._warned.get(user.id, 0.0) > self.limiter.window:
            self._warned[user.id] = now
            if isinstance(event, Message):
                await event.answer(THROTTLE_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(THROTTLE_TEXT, show_alert=False)
        return None
