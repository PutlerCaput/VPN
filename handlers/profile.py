"""Profile, status and configuration delivery."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from handlers.common import send_config, status_text
from keyboards.main_menu import BTN_CONFIG, BTN_PROFILE, app_kb, main_menu_kb
from services.container import Services
from services.marzban import MarzbanError
from services.provisioning import ProvisioningError

logger = logging.getLogger(__name__)
router = Router(name="profile")


@router.message(F.text == BTN_PROFILE)
@router.message(Command("status"))
async def show_profile(message: Message, services: Services) -> None:
    if message.from_user is None:
        return
    user = await services.db.get_user(message.from_user.id)
    if user is None:
        await message.answer("Нажмите /start, чтобы начать.")
        return
    invited = await services.db.count_referrals(user["telegram_id"])
    await message.answer(
        f"👤 <b>Профиль</b>\n\nID: <code>{user['telegram_id']}</code>\n"
        f"{status_text(user)}\n<b>Приглашено:</b> {invited}",
        reply_markup=main_menu_kb(),
    )
    await message.answer(
        "Подробнее — в приложении:", reply_markup=app_kb(services.settings.tma_url)
    )


@router.message(F.text == BTN_CONFIG)
@router.message(Command("config"))
async def show_config(message: Message, services: Services) -> None:
    if message.from_user is None:
        return
    await deliver_config(message, message.from_user.id, services)


async def deliver_config(message: Message, owner_id: int, services: Services) -> None:
    """Callbacks must use their sender, not the author of the bot's message."""
    if message.chat.type != "private" or message.chat.id != owner_id:
        return
    user = await services.db.get_user(owner_id)
    if user is None or not await services.db.has_accepted_offer(owner_id):
        await message.answer("Сначала примите оферту: нажмите /start.")
        return
    if user["status"] != "active":
        await message.answer(
            f"{status_text(user)}\n\nКонфигурация доступна только при активной аренде. "
            "Продлить — «💳 Оплата».",
        )
        return
    try:
        link = await services.provisioning.fresh_config(user["telegram_id"])
    except (MarzbanError, ProvisioningError):
        logger.warning(
            "fresh_config_failed", extra={"telegram_id": user["telegram_id"]}
        )
        link = user["config_link"]
    if not link:
        await message.answer(
            "⚠️ Узел временно недоступен. Попробуйте через несколько минут."
        )
        return
    await send_config(message, link, user["expires_at"])
