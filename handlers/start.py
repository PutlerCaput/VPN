"""/start: registration, referral deep links, offer acceptance and trial."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import TIER_PRICES_RUB, TIERS, all_periods
from handlers.common import DISCLAIMER, is_trial, send_config, status_text
from handlers.payments import show_channels
from keyboards.main_menu import accept_offer_kb, app_kb, connect_app_kb, main_menu_kb
from middlewares.banned import BANNED_TEXT
from services.container import Services
from services.security import html_escape, sanitize_username

logger = logging.getLogger(__name__)
router = Router(name="start")

OFFER_PROMPT = (
    "<b>Cyberdefency — защищённое подключение в публичных сетях.</b>\n\n"
    "Открытый Wi-Fi в кафе, отеле или аэропорту — удобное место для перехвата "
    "данных. Бот выдаст личную конфигурацию: трафик между вашим устройством "
    "и нашим сервером будет зашифрован.\n\n"
    "• <b>24 часа бесплатно</b>, без оплаты и привязки карты\n"
    "• Подключение за пару минут: инструкция для вашего устройства прямо в боте\n"
    "• Защита от вредоносных и фишинговых сайтов на стороне сервера\n"
    "• Дальше — от 299 ₽ в месяц, оплата через СБП\n\n"
    "Прежде чем начать, прочитайте оферту и политику обработки персональных "
    "данных: в них условия, ограничения и то, какие данные мы храним. "
    "Сканирование чужих сетей, DDoS, вредоносное ПО и мошенничество запрещены.\n\n"
    f"{DISCLAIMER}"
)


WELCOME_IMAGE = (
    Path(__file__).resolve().parent.parent
    / "branding"
    / "telegram"
    / "welcome-1280x720.jpg"
)
# Telegram's file_id after the first upload, so the image is sent, not re-uploaded.
_welcome_file: dict[str, str] = {}


async def send_welcome_image(message: Message) -> None:
    """The logo before the first greeting. A missing file or an upload error
    never blocks the start."""
    photo: str | FSInputFile
    if "file_id" in _welcome_file:
        photo = _welcome_file["file_id"]
    elif WELCOME_IMAGE.is_file():
        photo = FSInputFile(WELCOME_IMAGE)
    else:
        return
    try:
        sent = await message.answer_photo(photo)
    except TelegramAPIError:
        logger.warning("welcome_image_failed")
        return
    sizes = getattr(sent, "photo", None)
    if sizes and isinstance(sizes[-1].file_id, str):
        _welcome_file.setdefault("file_id", sizes[-1].file_id)


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
    ``pay_<tier>_<months>``. A period without a tier returns tier None; the
    caller picks the first tier on sale.
    """
    if not arg:
        return None, None, None, False
    if arg == "pay":
        return None, None, None, True
    if arg.startswith("pay_"):
        tier: str | None = None
        months: int | None = None
        for part in arg[4:].split("_")[:2]:
            if part.isdigit() and int(part) in all_periods():
                months = int(part)
            elif part in TIERS:
                tier = part
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
        await send_welcome_image(message)
        await message.answer(
            f"{welcome_message(tg.first_name)}\n\n{OFFER_PROMPT}",
            reply_markup=accept_offer_kb(services.settings.offer_url),
        )
        return

    await message.answer(
        f"{welcome_message(tg.first_name, returning=True)}\n\n"
        f"{status_text(db_user, trial=await is_trial(db, db_user))}",
        reply_markup=main_menu_kb(),
    )
    await message.answer(
        "Управлять узлом удобнее в приложении:",
        reply_markup=app_kb(services.settings.tma_url),
    )
    if pay_requested:
        if pay_months is not None and pay_tier is None:
            pay_tier = services.settings.default_tier
        if (
            pay_tier is not None
            and pay_months is not None
            and pay_months not in TIER_PRICES_RUB[pay_tier]
        ):
            pay_months = None
        await show_channels(message, services, pay_months, pay_tier)


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
            "Пробный период на этом аккаунте уже использован. Продолжить защиту "
            "можно от 299 ₽ в месяц — раздел «💳 Оплата».",
            reply_markup=main_menu_kb(),
        )
        return
    trial_gb = services.settings.trial_traffic_gb
    await message.answer(
        "🎉 <b>Готово! 24 часа бесплатной защиты уже идут.</b>"
        + (f" Лимит трафика на пробный период: {trial_gb} ГБ." if trial_gb else "")
        + "\n\nНиже — ваша личная конфигурация. Выберите устройство, и бот покажет, "
        "куда её вставить: обычно это занимает пару минут.",
        reply_markup=main_menu_kb(),
    )
    if result.config_link:
        await send_config(
            message,
            result.config_link,
            result.expires_at,
            awg=services.awg is not None,
            connect=connect_app_kb(
                services.settings.public_base_url,
                services.settings.subscription_base_url,
                result.config_link,
            ),
        )
