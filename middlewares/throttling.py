"""Per-user sliding-window throttling for bot updates (30 req/min)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from services.security import SlidingWindowLimiter

THROTTLE_TEXT = "⏳ Слишком много запросов. Подождите минуту."
# Above this many remembered warnings, expired ones are dropped.
WARNED_PRUNE_SIZE = 1000


def is_payment_service_message(event: TelegramObject) -> bool:
    return isinstance(event, Message) and (
        getattr(event, "successful_payment", None) is not None
        or getattr(event, "refunded_payment", None) is not None
    )


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limiter: SlidingWindowLimiter | None = None) -> None:
        # Not "limiter or ...": a limiter with no hits yet has len() == 0, is
        # falsy, and was silently replaced by the default one.
        self.limiter = (
            limiter
            if limiter is not None
            else SlidingWindowLimiter(limit=30, window_seconds=60)
        )
        self._warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or is_payment_service_message(event):
            # Telegram has already taken the payment: never drop its receipt.
            return await handler(event, data)
        if self.limiter.hit(str(user.id)):
            return await handler(event, data)
        now = time.monotonic()
        # Warn at most once per window so throttling itself cannot be abused.
        if now - self._warned.get(user.id, 0.0) > self.limiter.window:
            self._warned[user.id] = now
            if len(self._warned) > WARNED_PRUNE_SIZE:
                cutoff = now - self.limiter.window
                self._warned = {
                    uid: at for uid, at in self._warned.items() if at > cutoff
                }
            if isinstance(event, Message):
                await event.answer(THROTTLE_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(THROTTLE_TEXT, show_alert=False)
        return None
