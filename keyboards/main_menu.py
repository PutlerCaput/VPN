"""Reply and inline keyboards."""

from __future__ import annotations

from urllib.parse import quote

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from config import (
    DEFAULT_TIER,
    TIER_PRICES_RUB,
    TIER_PRICES_USDT,
    TIERS,
    savings_percent,
)

BTN_CONFIG = "🔐 Моя конфигурация"
BTN_SETUP = "📱 Подключить устройство"
BTN_PAY = "💳 Оплата"
BTN_PROFILE = "👤 Профиль"
BTN_REFERRAL = "🎁 Рефералы"
BTN_SUPPORT = "💬 Поддержка"
BTN_LEGAL = "📄 Оферта"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SETUP), KeyboardButton(text=BTN_CONFIG)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_PAY)],
            [KeyboardButton(text=BTN_SUPPORT), KeyboardButton(text=BTN_LEGAL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def accept_offer_kb(full_offer_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✅ Принимаю", callback_data="offer:accept")]]
    rows.append(
        [
            InlineKeyboardButton(
                text="📄 Полный текст оферты", callback_data="offer:full"
            )
        ]
    )
    if full_offer_url:
        rows.append(
            [InlineKeyboardButton(text="🌐 Оферта на сайте", url=full_offer_url)]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def app_kb(tma_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 Открыть приложение", web_app=WebAppInfo(url=tma_url)
                )
            ],
        ],
    )


def channel_kb(
    months: int | None = None, tier: str | None = None
) -> InlineKeyboardMarkup:
    """Payment channel choice; a deep link may pre-select the tier and the period."""
    suffix = f":{tier}:{months}" if tier and months else (f":{tier}" if tier else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 ЮKassa: СБП или карта",
                    callback_data=f"chan:sbp{suffix}",
                )
            ],
        ],
    )


def _from_price(method: str, tier: str) -> str:
    if method == "sbp":
        return f"от {TIER_PRICES_RUB[tier][1]} ₽/мес"
    return f"от {TIER_PRICES_USDT[tier][1]} USDT/мес"


def tiers_kb(method: str, enabled: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{TIERS[key].title} — {_from_price(method, key)}",
                callback_data=f"tier:{method}:{key}",
            )
        ]
        for key in TIERS
        if key in enabled
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="chan:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariffs_kb(method: str, tier: str = DEFAULT_TIER) -> InlineKeyboardMarkup:
    rows = []
    for months in sorted(TIER_PRICES_RUB[tier]):
        price = (
            f"{TIER_PRICES_RUB[tier][months]} ₽"
            if method == "sbp"
            else f"{TIER_PRICES_USDT[tier][months]} USDT"
        )
        badge = savings_percent(tier, months)
        label = f"{months} мес. — {price}" + (f" · выгода {badge}%" if badge else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"pay:{method}:{tier}:{months}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ К тарифам", callback_data=f"chan:{method}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sbp_payment_kb(payment_url: str, payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏦 Открыть в приложении банка", url=payment_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил", callback_data=f"paid:{payment_id}"
                )
            ],
        ],
    )


def yookassa_payment_kb(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить", url=payment_url)]],
    )


def usdt_payment_kb(pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💎 Оплатить USDT", url=pay_url)]],
    )


def share_kb(referral_link: str) -> InlineKeyboardMarkup:
    text = (
        "Шифрую подключение в публичных Wi-Fi через свой технический узел. "
        "Попробуй сутки бесплатно:"
    )
    query = f"url={quote(referral_link, safe='')}&text={quote(text, safe='')}"
    share_url = f"https://t.me/share/url?{query}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)]
        ],
    )
