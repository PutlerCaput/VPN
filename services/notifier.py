"""Delivers user notifications over Telegram and, when subscribed, Web Push."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from services.brand_animation import send_branded
from services.push import PushService

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(
        self,
        bot: Bot,
        push: PushService,
        *,
        brand_animation_enabled: bool = True,
    ) -> None:
        self.bot = bot
        self.push = push
        self.brand_animation_enabled = brand_animation_enabled

    async def telegram(
        self, telegram_id: int, text: str, *, animation: bool = False
    ) -> bool:
        if telegram_id < 0:
            # An account made on the site by email: there is no chat to write to.
            return False
        try:
            if animation:
                await send_branded(
                    text,
                    enabled=self.brand_animation_enabled,
                    bot=self.bot,
                    chat_id=telegram_id,
                    disable_web_page_preview=True,
                )
            else:
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
        animation: bool = False,
    ) -> bool:
        delivered = await self.telegram(telegram_id, text, animation=animation)
        if push_title:
            try:
                await self.push.send(telegram_id, push_title, push_body or "", url)
            except Exception:
                logger.exception("push_send_failed", extra={"telegram_id": telegram_id})
        return delivered
