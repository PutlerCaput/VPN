"""Reply and inline keyboards."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
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
    stars_price,
)

DEFAULT_RUB_PER_STAR = Decimal("1.60")
CHANNEL_BUTTONS = {
    "stars": "⭐ Telegram Stars",
    "sbp": "💳 Оплата через СБП",
}

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
    rows.append(
        [
            InlineKeyboardButton(
                text="🔒 Политика обработки персональных данных",
                callback_data="privacy:full",
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
    months: int | None = None,
    tier: str | None = None,
    methods: Sequence[str] = ("sbp",),
) -> InlineKeyboardMarkup:
    """Payment channel choice; a deep link may pre-select the tier and the period."""
    suffix = f":{tier}:{months}" if tier and months else (f":{tier}" if tier else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CHANNEL_BUTTONS[method],
                    callback_data=f"chan:{method}{suffix}",
                )
            ]
            for method in methods
            if method in CHANNEL_BUTTONS
        ],
    )


def price_text(
    method: str,
    tier: str,
    months: int,
    rub_per_star: Decimal = DEFAULT_RUB_PER_STAR,
) -> str:
    if method == "stars":
        return f"{stars_price(tier, months, rub_per_star)} ⭐"
    if method == "usdt":
        return f"{TIER_PRICES_USDT[tier][months]} USDT"
    return f"{TIER_PRICES_RUB[tier][months]} ₽"


def _from_price(method: str, tier: str, rub_per_star: Decimal) -> str:
    return f"от {price_text(method, tier, 1, rub_per_star)}/мес"


def tiers_kb(
    method: str,
    enabled: list[str],
    rub_per_star: Decimal = DEFAULT_RUB_PER_STAR,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{TIERS[key].title} — {_from_price(method, key, rub_per_star)}",
                callback_data=f"tier:{method}:{key}",
            )
        ]
        for key in TIERS
        if key in enabled
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="chan:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariffs_kb(
    method: str,
    tier: str = DEFAULT_TIER,
    rub_per_star: Decimal = DEFAULT_RUB_PER_STAR,
    *,
    autorenew: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if autorenew and method == "stars" and 1 in TIER_PRICES_RUB[tier]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"🔁 Каждый месяц с автопродлением — "
                        f"{price_text('stars', tier, 1, rub_per_star)}"
                    ),
                    callback_data=f"autorenew:{tier}",
                )
            ]
        )
    best = max(TIER_PRICES_RUB[tier], key=lambda m: savings_percent(tier, m))
    for months in sorted(TIER_PRICES_RUB[tier]):
        price = price_text(method, tier, months, rub_per_star)
        badge = savings_percent(tier, months)
        label = f"{months} мес. — {price}"
        if method == "sbp" and months > 1:
            # Short form for narrow phone screens: price per month and the discount.
            label += f" · {round(TIER_PRICES_RUB[tier][months] / months)} ₽/мес"
            label += f" · −{badge}%" if badge else ""
        elif badge:
            label += f" · выгода {badge}%"
        if badge and months == best:
            label = f"🔥 {label}"
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


def config_actions_kb(awg: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🔄 Сбросить ссылку (отключить старые устройства)",
                callback_data="cfg:revoke",
            )
        ]
    ]
    if awg:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🛰 AmneziaWG: отдельный ключ на устройство",
                    callback_data="awg:menu",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def revoke_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, выдать новую ссылку", callback_data="cfg:revoke:yes"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="cfg:revoke:no")],
        ],
    )


# Apps that take a subscription link through their own URL scheme. The page on
# the site (connect.html) checks the link before it hands the link to an app.
CONNECT_APPS: tuple[tuple[str, str], ...] = (
    # The one app offered with a one-tap link; others get the copy and QR paths.
    ("hiddify", "💻 Подключить в Hiddify"),
)
CONNECT_PAGE = "/connect.html"


def connect_app_kb(
    public_base_url: str | None,
    subscription_base_url: str | None,
    link: str | None,
) -> InlineKeyboardMarkup | None:
    """One-tap buttons that open a VPN app with this subscription link.

    Only a link on this service's subscription host gets buttons: the connect
    page refuses any other address, so the bot does not offer one either.
    """
    if not (public_base_url and subscription_base_url and link):
        return None
    site = public_base_url.rstrip("/")
    base = subscription_base_url.rstrip("/")
    if not site.startswith("https://") or not link.startswith(f"{base}/sub/"):
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                url=f"{site}{CONNECT_PAGE}#app={app}&sub={quote(link, safe='')}",
            )
        ]
        for app, label in CONNECT_APPS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_link_kb(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Оформить автопродление", url=link)]
        ],
    )


def autorenew_cancel_kb(subscription_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отключить автопродление",
                    callback_data=f"autorenew:cancel:{subscription_id}",
                )
            ]
        ],
    )


def yookassa_payment_kb(
    payment_url: str, payment_id: str | None = None
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💳 Оплатить через СБП", url=payment_url)]]
    if payment_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату", callback_data=f"paycheck:{payment_id}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
