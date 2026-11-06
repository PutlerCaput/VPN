"""Connection wizard controls, without credentials in callback data or URLs."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.connection_guide import AMNEZIA_GUIDE, GUIDES


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


def guide_kb(key: str) -> InlineKeyboardMarkup:
    guide = AMNEZIA_GUIDE if key == "amnezia" else GUIDES[key]
    config_action = "awg:menu" if key == "amnezia" else "setup:config"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Установить {guide.client}", url=guide.download_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="Получить мою конфигурацию", callback_data=config_action
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
