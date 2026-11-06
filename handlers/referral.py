"""Referral program: +15 days to the inviter after the invitee's first payment."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from keyboards.main_menu import BTN_REFERRAL, share_kb
from services.container import Services

router = Router(name="referral")


@router.message(F.text == BTN_REFERRAL)
@router.message(Command("referral"))
async def show_referral(message: Message, services: Services) -> None:
    if message.from_user is None:
        return
    telegram_id = message.from_user.id
    settings = services.settings
    link = f"{settings.referral_link_base}{telegram_id}"
    invited = await services.db.count_referrals(telegram_id)
    bonus = await services.db.referral_bonus_days(telegram_id)
    if not settings.referral_acquisition_enabled:
        await message.answer(
            "Новые приглашения по реферальной программе отключены.\n"
            "Ранее начисленные дни сохраняются.\n\n"
            f"Приглашено ранее: <b>{invited}</b>\n"
            f"Начислено бонусных дней: <b>{bonus}</b>"
        )
        return
    await message.answer(
        "🎁 <b>Реферальная программа</b>\n\n"
        f"Когда приглашённый вами пользователь впервые оплатит аренду, вы получите "
        f"<b>+{settings.referral_inviter_bonus_days} дней</b>, а он — "
        f"<b>+{settings.referral_invitee_bonus_days} дня</b>.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{invited}</b>\nНачислено бонусных дней: <b>{bonus}</b>",
        reply_markup=share_kb(link),
    )
