"""Referral program: +15 days to the inviter after the invitee's first payment."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from keyboards.main_menu import BTN_REFERRAL, share_kb
from services.container import Services

router = Router(name="referral")


def days_text(n: int) -> str:
    """ "1 день", "3 дня", "15 дней"."""
    tail = n % 100
    if 11 <= tail <= 14:
        word = "дней"
    elif n % 10 == 1:
        word = "день"
    elif 2 <= n % 10 <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


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
    inviter = settings.referral_inviter_bonus_days
    invitee = settings.referral_invitee_bonus_days
    await message.answer(
        "🎁 <b>Защищайтесь вместе с друзьями</b>\n\n"
        "Отправьте другу ссылку: он получит 24 часа бесплатно, а после его "
        f"первой оплаты вы получите <b>+{days_text(inviter)}</b>, "
        f"а он — <b>+{days_text(invitee)}</b> к подписке.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{invited}</b>\nНачислено бонусных дней: <b>{bonus}</b>",
        reply_markup=share_kb(link),
    )
