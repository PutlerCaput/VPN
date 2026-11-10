"""Connection wizard controls, without credentials in callback data or URLs."""

from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from services.connection_guide import AMNEZIA_GUIDE, GUIDES, ClientApp

# The Bot API copies at most this many characters with a copy button.
COPY_LIMIT = 256
DIRECT_BUTTON = "🇷🇺 Российские сайты напрямую"


def setup_kb(awg: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=g.title, callback_data=f"setup:guide:{key}")
        for key, g in GUIDES.items()
    ]
    if awg:
        buttons.append(
            InlineKeyboardButton(
                text=f"🛰 {AMNEZIA_GUIDE.title}", callback_data="setup:guide:amnezia"
            )
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(
                text="🛡 Защита при обрыве", callback_data="setup:killswitch"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="Не получается", callback_data="setup:help")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_apps_kb(device_key: str) -> InlineKeyboardMarkup:
    """The apps offered for one device; tapping one sends the link and the steps."""
    device = GUIDES[device_key]
    rows = [
        [
            InlineKeyboardButton(
                text=app.name, callback_data=f"setup:app:{device_key}:{app.key}"
            )
        ]
        for app in device.apps
    ]
    rows.append(
        [InlineKeyboardButton(text="Другое устройство", callback_data="setup:choose")]
    )
    rows.append(
        [InlineKeyboardButton(text="Не получается", callback_data="setup:help")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_guide_kb(
    device_key: str, app: ClientApp, link: str | None, auto_direct: bool = False
) -> InlineKeyboardMarkup:
    """Under the steps: copy the link, install the app, another app or device.

    auto_direct: the app gets "Russian sites directly" with the subscription,
    so there is nothing to show behind the routing button.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if link and len(link) <= COPY_LIMIT:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Скопировать ссылку", copy_text=CopyTextButton(text=link)
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text=f"Установить {app.name}", url=app.download_url)]
    )
    if link:
        rows.append(
            [
                InlineKeyboardButton(
                    text="QR-код и сброс ссылки", callback_data="setup:config"
                )
            ]
        )
    if not auto_direct:
        rows.append(
            [
                InlineKeyboardButton(
                    text=DIRECT_BUTTON,
                    callback_data=f"setup:direct:{device_key}:{app.key}",
                )
            ]
        )
    if len(GUIDES[device_key].apps) > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Другое приложение",
                    callback_data=f"setup:guide:{device_key}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Другое устройство", callback_data="setup:choose")]
    )
    rows.append(
        [InlineKeyboardButton(text="Не получается", callback_data="setup:help")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def direct_kb(device_key: str, app: ClientApp) -> InlineKeyboardMarkup:
    """Under the routing steps: back to the app's guide, or ask for help."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"‹ Инструкция {app.name}",
                    callback_data=f"setup:app:{device_key}:{app.key}",
                )
            ],
            [InlineKeyboardButton(text="Не получается", callback_data="setup:help")],
        ]
    )


def guide_kb(key: str) -> InlineKeyboardMarkup:
    """The AmneziaVPN page; any other device key shows that device's apps."""
    if key != "amnezia":
        return client_apps_kb(key)
    guide = AMNEZIA_GUIDE
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Установить {guide.client}", url=guide.download_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="Получить мою конфигурацию", callback_data="awg:menu"
                )
            ],
            [InlineKeyboardButton(text="Не получается", callback_data="setup:help")],
            [
                InlineKeyboardButton(
                    text="Другое устройство", callback_data="setup:choose"
                )
            ],
        ]
    )


def help_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Написать в поддержку", callback_data="support:write"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Инструкция подключения", callback_data="setup:choose"
                )
            ],
        ]
    )
