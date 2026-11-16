"""/start: registration, referral deep links, offer acceptance and trial."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from config import TIER_PRICES_RUB, TIERS, all_periods
from handlers.common import DISCLAIMER, is_trial, send_config, status_text
from handlers.payments import show_channels
from keyboards.main_menu import accept_offer_kb, app_kb, main_menu_kb
from middlewares.banned import BANNED_TEXT
from services.brand_animation import send_branded
from services.container import Services
from services.security import html_escape, sanitize_username

router = Router(name="start")

_TIER = TIERS["standard"]
_PRICE = TIER_PRICES_RUB["standard"][1]

OFFER_PROMPT = (
    "<b>Cyberdefency — личное защищённое подключение за пару минут.</b>\n\n"
    "Открытый Wi-Fi в кафе, отеле или аэропорту — удобное место для перехвата "
    "данных. Бот выдаст вашу личную ссылку: трафик между устройством и нашим "
    "сервером в Финляндии будет зашифрован.\n\n"
    "• <b>24 часа бесплатно</b> — без оплаты и привязки карты\n"
    "• Инструкция под ваше устройство прямо в боте: iPhone, Android, Windows, macOS\n"
    "• Два протокола на выбор: VLESS + Reality и AmneziaWG\n"
    f"• Дальше — {_PRICE} ₽ за 30 дней: {_TIER.devices} устройства и "
    f"{_TIER.traffic_gb} ГБ трафика. Оплата через СБП или Telegram Stars, "
    "списания — только по вашему решению\n\n"
    "Перед началом прочитайте оферту и политику обработки персональных данных: "
    "там условия, ограничения и то, какие данные мы храним. Сканирование чужих "
    "сетей, DDoS, вредоносное ПО и мошенничество запрещены.\n\n"
    f"{DISCLAIMER}"
)


TRIALS_PAUSED_TEXT = (
    "Оферта принята. Пробный доступ сейчас временно недоступен: сервер близок "
    "к месячному лимиту трафика. Подписка работает как обычно — оформить её "
    "можно в разделе «💳 Оплата»."
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

    animation = services.settings.brand_animation_enabled
    if not await db.has_accepted_offer(tg.id):
        await send_branded(
            f"{welcome_message(tg.first_name)}\n\n{OFFER_PROMPT}",
            enabled=animation,
            reply_markup=accept_offer_kb(services.settings.offer_url),
            message=message,
            still_fallback=True,
        )
        return

    await send_branded(
        f"{welcome_message(tg.first_name, returning=True)}\n\n"
        f"{status_text(db_user, trial=await is_trial(db, db_user))}",
        enabled=animation,
        reply_markup=main_menu_kb(),
        message=message,
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
    if result.reason == "trials_paused":
        await message.answer(TRIALS_PAUSED_TEXT, reply_markup=main_menu_kb())
        return
    if not result.ok:
        await message.answer(
            "⚠️ Не удалось подготовить узел. Попробуйте ещё раз через пару минут: "
            "нажмите /start.",
        )
        return
    if result.reason == "already_used":
        await message.answer(
            "Пробный период на этом аккаунте уже использован. Продолжить можно "
            f"за {_PRICE} ₽ в месяц — раздел «💳 Оплата».",
            reply_markup=main_menu_kb(),
        )
        return
    trial_gb = services.settings.trial_traffic_gb
    await send_branded(
        "🎉 <b>Готово! 24 часа бесплатной защиты уже идут.</b>"
        + (f" Лимит трафика на пробный период: {trial_gb} ГБ." if trial_gb else "")
        + "\n\nНиже — ваша личная конфигурация. Выберите устройство, и бот покажет, "
        "куда её вставить: обычно это занимает пару минут.",
        enabled=services.settings.brand_animation_enabled,
        reply_markup=main_menu_kb(),
        message=message,
    )
    if result.config_link:
        await send_config(
            message,
            result.config_link,
            result.expires_at,
            awg=services.awg is not None,
        )
