"""Profile, status and configuration delivery."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from handlers.common import is_trial, send_config, status_text
from keyboards.main_menu import (
    BTN_CONFIG,
    BTN_PROFILE,
    app_kb,
    main_menu_kb,
    revoke_confirm_kb,
)
from services.container import Services
from services.marzban import MarzbanError
from services.provisioning import ProvisioningError
from services.security import SlidingWindowLimiter

logger = logging.getLogger(__name__)
router = Router(name="profile")
# A new link disconnects every device; a few resets a day are plenty.
REVOKE_LIMITER = SlidingWindowLimiter(limit=3, window_seconds=86400)


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
    traffic = (
        await services.provisioning.traffic_status(user)
        if user["status"] == "active"
        else None
    )
    referrals = (
        f"\n<b>Приглашено:</b> {invited}"
        if services.settings.referral_acquisition_enabled or invited
        else ""
    )
    await message.answer(
        f"👤 <b>Профиль</b>\n\nID: <code>{user['telegram_id']}</code>\n"
        f"{status_text(user, traffic, trial=await is_trial(services.db, user))}"
        f"{referrals}",
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


@router.callback_query(F.data == "cfg:revoke")
async def cb_revoke_ask(callback: CallbackQuery) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    await callback.answer()
    await callback.message.answer(
        "🔄 <b>Сбросить ссылку подключения?</b>\n\n"
        "Бот выдаст новую ссылку, а все устройства со старой отключатся. "
        "Затем импортируйте новую ссылку на своих устройствах (не больше, "
        "чем разрешает тариф).",
        reply_markup=revoke_confirm_kb(),
    )


@router.callback_query(F.data.in_({"cfg:revoke:yes", "cfg:revoke:no"}))
async def cb_revoke(callback: CallbackQuery, services: Services) -> None:
    message = callback.message
    if (
        not isinstance(message, Message)
        or message.chat.type != "private"
        or message.chat.id != callback.from_user.id
    ):
        await callback.answer()
        return
    await message.edit_reply_markup(reply_markup=None)
    if callback.data == "cfg:revoke:no":
        await callback.answer("Ссылка не изменена")
        return
    owner_id = callback.from_user.id
    if not REVOKE_LIMITER.hit(str(owner_id)):
        await callback.answer(
            "Ссылку можно сбрасывать не чаще 3 раз в сутки.", show_alert=True
        )
        return
    await callback.answer()
    try:
        link = await services.provisioning.revoke_link(owner_id)
    except ProvisioningError:
        await message.answer(
            "Сбросить ссылку можно только при активной подписке. Продлить — "
            "«💳 Оплата»."
        )
        return
    except MarzbanError:
        logger.warning("revoke_link_failed", extra={"telegram_id": owner_id})
        await message.answer(
            "⚠️ Узел сейчас недоступен, ссылка не изменена. Попробуйте позже."
        )
        return
    user = await services.db.get_user(owner_id)
    await message.answer("✅ Старая ссылка отключена. Новая конфигурация:")
    await send_config(message, link, user["expires_at"] if user else None)
