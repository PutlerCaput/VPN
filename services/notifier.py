"""Delivers user notifications over Telegram and, when subscribed, Web Push."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from services.push import PushService

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, push: PushService) -> None:
        self.bot = bot
        self.push = push

    async def telegram(self, telegram_id: int, text: str) -> bool:
        if telegram_id < 0:
            # An account made on the site by email: there is no chat to write to.
            return False
        try:
            await self.bot.send_message(
                telegram_id, text, disable_web_page_preview=True
            )
            return True
        except TelegramForbiddenError:
            logger.info("user_blocked_bot", extra={"telegram_id": telegram_id})
        except TelegramAPIError:
            logger.exception("telegram_send_failed", extra={"telegram_id": telegram_id})
        return False

    async def notify(
        self,
        telegram_id: int,
        text: str,
        *,
        push_title: str | None = None,
        push_body: str | None = None,
        url: str = "/",
    ) -> bool:
        delivered = await self.telegram(telegram_id, text)
        if push_title:
            try:
                await self.push.send(telegram_id, push_title, push_body or "", url)
            except Exception:
                logger.exception("push_send_failed", extra={"telegram_id": telegram_id})
        return delivered
