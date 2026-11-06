"""/start: registration, referral deep links, offer acceptance and trial."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from config import DEFAULT_TIER, TIERS
from handlers.common import DISCLAIMER, send_config, status_text
from handlers.payments import show_channels
from keyboards.main_menu import accept_offer_kb, app_kb, main_menu_kb
from middlewares.banned import BANNED_TEXT
from services.container import Services
from services.security import html_escape, sanitize_username

logger = logging.getLogger(__name__)
router = Router(name="start")

OFFER_PROMPT = (
    "<b>Cyberdefency — управление личным подключением.</b>\n\n"
    "Здесь можно получить конфигурацию, открыть инструкцию для устройства, "
    "проверить срок подписки и написать в поддержку.\n\n"
    "Сначала прочитайте оферту: в ней указаны условия, ограничения "
    "и обработка данных. Несанкционированный доступ, сканирование чужих сетей, "
    "DDoS, вредоносное ПО и мошенничество запрещены.\n\n"
    f"{DISCLAIMER}\n\n"
    "После принятия оферты вы получите <b>24 часа бесплатного доступа</b>."
)


def welcome_message(first_name: str, *, returning: bool = False) -> str:
    """Build a personalized Telegram-HTML greeting without trusting profile text."""
    name = html_escape(first_name.strip()) if first_name.strip() else "друг"
    greeting = "Рады снова видеть" if returning else "Добро пожаловать"
    return f"👋 <b>{greeting}, {name}!</b>"


def parse_start_arg(
    arg: str | None, own_id: int
) -> tuple[int | None, str | None, int | None, bool]:
    """Returns (referrer_id, pay_tier, pay_months, pay_requested).

    Payment deep links: ``pay``, ``pay_<months>``, ``pay_<tier>`` and
    ``pay_<tier>_<months>``; ``pay_<months>`` predates tiers and means the basic tier.
    """
    if not arg:
        return None, None, None, False
    if arg == "pay":
        return None, None, None, True
    if arg.startswith("pay_"):
        tier: str | None = None
        months: int | None = None
        for part in arg[4:].split("_")[:2]:
            if part.isdigit() and int(part) in (1, 3, 6, 12):
                months = int(part)
            elif part in TIERS:
                tier = part
        if months is not None and tier is None:
            tier = DEFAULT_TIER
        return None, tier, months, True
    if arg.isdigit() and len(arg) <= 20:
        referrer = int(arg)
        return (referrer if referrer != own_id else None), None, None, False
    return None, None, None, False


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    services: Services,
    db_user: dict[str, Any] | None = None,
) -> None:
    if message.from_user is None:
        return
    tg = message.from_user
    db = services.db
    referrer_id, pay_tier, pay_months, pay_requested = parse_start_arg(
        command.args, tg.id
    )
    if not services.settings.referral_acquisition_enabled:
        referrer_id = None
    username = sanitize_username(tg.username)

    if db_user is None:
        if referrer_id is not None and await db.get_user(referrer_id) is None:
            referrer_id = None
        await db.create_user(tg.id, username, referrer_id)
        db_user = await db.get_user(tg.id)
    else:
        await db.update_profile(tg.id, username, tg.first_name)
    if db_user is None:
        return

    if db_user["status"] == "banned":
        await message.answer(BANNED_TEXT)
        return

    if not await db.has_accepted_offer(tg.id):
        await message.answer(
            f"{welcome_message(tg.first_name)}\n\n{OFFER_PROMPT}",
            reply_markup=accept_offer_kb(services.settings.offer_url),
        )
        return

    await message.answer(
        f"{welcome_message(tg.first_name, returning=True)}\n\n{status_text(db_user)}",
        reply_markup=main_menu_kb(),
    )
    await message.answer(
        "Управлять узлом удобнее в приложении:",
        reply_markup=app_kb(services.settings.tma_url),
    )
    if pay_requested:
        await show_channels(message, pay_months, pay_tier)


@router.callback_query(F.data == "offer:accept")
async def cb_accept_offer(callback: CallbackQuery, services: Services) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    await callback.answer("Оферта принята")
    await callback.message.edit_reply_markup(reply_markup=None)
    result = await services.provisioning.accept_offer_and_start_trial(
        callback.from_user.id
    )
    message = callback.message
    if result.reason == "banned":
        await message.answer(BANNED_TEXT)
        return
    if not result.ok:
        await message.answer(
            "⚠️ Не удалось подготовить узел. Попробуйте ещё раз через пару минут: "
            "нажмите /start.",
        )
        return
    if result.reason == "already_used":
        await message.answer(
            "Пробный период уже был использован. "
            "Продлить аренду можно в разделе «💳 Оплата».",
            reply_markup=main_menu_kb(),
        )
        return
    await message.answer(
        "🎉 Пробный доступ на 24 часа активирован!", reply_markup=main_menu_kb()
    )
    if result.config_link:
        await send_config(message, result.config_link, result.expires_at)
