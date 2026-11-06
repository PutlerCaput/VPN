"""AmneziaWG devices: one key per device, added and removed by the subscriber."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.awg import AwgError, AwgInactiveError, AwgLimitError
from services.connection_guide import AMNEZIA_APP_URL
from services.container import Services
from services.qr import render_qr_png
from services.security import html_escape

logger = logging.getLogger(__name__)
router = Router(name="amnezia")

INTRO = (
    "🛰 <b>AmneziaWG</b> — второй протокол с маскировкой трафика от блокировок. "
    "У каждого устройства свой ключ: его можно удалить, не трогая остальные.\n\n"
    "Подходит для приложения AmneziaVPN (Android, iPhone, Windows, macOS, Linux), "
    "приложений AmneziaWG и роутеров с поддержкой AmneziaWG. Трафик входит "
    "в общий лимит тарифа."
)
NOT_CONNECTED = (
    "AmneziaWG пока не подключён на сервере. Пользуйтесь конфигурацией из "
    "«🔐 Моя конфигурация»."
)
UNAVAILABLE = "⚠️ Сервер AmneziaWG временно недоступен. Попробуйте позже."


def menu_kb(devices: list[dict[str, Any]], can_add: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"📥 {device['name']}", callback_data=f"awg:get:{device['id']}"
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"awg:del:{device['id']}"),
        ]
        for device in devices
    ]
    if can_add:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Добавить устройство", callback_data="awg:add"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="📲 Скачать AmneziaVPN", url=AMNEZIA_APP_URL)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_confirm_kb(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить ключ", callback_data=f"awg:delyes:{device_id}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="awg:menu")],
        ]
    )


async def _active_user(
    message: Message, owner_id: int, services: Services
) -> dict[str, Any] | None:
    user = await services.db.get_user(owner_id)
    if user is None or not await services.db.has_accepted_offer(owner_id):
        await message.answer("Сначала примите оферту: нажмите /start.")
        return None
    if user["status"] != "active":
        await message.answer(
            "AmneziaWG доступен при активной подписке. Продлить — «💳 Оплата»."
        )
        return None
    return user


async def show_menu(message: Message, owner_id: int, services: Services) -> None:
    awg = services.awg
    if awg is None:
        await message.answer(NOT_CONNECTED)
        return
    user = await _active_user(message, owner_id, services)
    if user is None:
        return
    devices = await awg.devices(owner_id)
    limit = await awg.device_limit(user)
    hint = (
        "\n\nНажмите «➕ Добавить устройство» и импортируйте файл в приложение."
        if not devices
        else ""
    )
    await message.answer(
        f"{INTRO}\n\nУстройств: {len(devices)} из {limit}.{hint}",
        reply_markup=menu_kb(devices, len(devices) < limit),
    )


async def send_device_config(
    message: Message, device_id: int, name: str, config: str
) -> None:
    await message.answer_document(
        BufferedInputFile(config.encode(), filename=f"cyberdefency-{device_id}.conf"),
        caption=(
            f"📥 <b>{html_escape(name)}</b>: файл AmneziaWG.\n"
            "В AmneziaVPN добавьте подключение из файла или отсканируйте QR-код "
            "ниже с другого экрана. Файл и QR — только для этого устройства: "
            "не пересылайте их."
        ),
    )
    await message.answer_photo(
        BufferedInputFile(render_qr_png(config), filename="amneziawg.png"),
        caption=f"QR-код AmneziaWG: {html_escape(name)}",
    )


@router.message(Command("amnezia"))
async def cmd_amnezia(message: Message, services: Services) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    await show_menu(message, message.from_user.id, services)


@router.callback_query(F.data.startswith("awg:"))
async def cb_awg(callback: CallbackQuery, services: Services) -> None:
    message = callback.message
    if (
        not isinstance(message, Message)
        or message.chat.type != "private"
        or message.chat.id != callback.from_user.id
    ):
        await callback.answer("Откройте личный чат с ботом.")
        return
    owner_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    device_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    awg = services.awg
    if awg is None:
        await callback.answer()
        await message.answer(NOT_CONNECTED)
        return
    await callback.answer()

    if action == "menu":
        await show_menu(message, owner_id, services)
    elif action == "add":
        if await _active_user(message, owner_id, services) is None:
            return
        try:
            device = await awg.add_device(owner_id)
            config = await awg.config_for(owner_id, int(device["id"]))
        except AwgLimitError:
            await message.answer(
                "Достигнут лимит устройств тарифа. Удалите ключ устройства, "
                "которым больше не пользуетесь, и добавьте новый."
            )
            return
        except AwgInactiveError:
            await message.answer("AmneziaWG доступен при активной подписке.")
            return
        except AwgError:
            logger.warning("awg_add_failed")
            await message.answer(UNAVAILABLE)
            return
        if config is not None:
            await send_device_config(
                message, int(device["id"]), str(device["name"]), config
            )
    elif action == "get" and device_id is not None:
        if await _active_user(message, owner_id, services) is None:
            return
        try:
            config = await awg.config_for(owner_id, device_id)
        except AwgError:
            await message.answer(UNAVAILABLE)
            return
        stored = await services.db.get_awg_device(device_id)
        if config is None or stored is None:
            await message.answer("Устройство не найдено: /amnezia.")
            return
        await send_device_config(message, device_id, str(stored["name"]), config)
    elif action == "del" and device_id is not None:
        target = await services.db.get_awg_device(device_id)
        if target is None or int(target["telegram_id"]) != owner_id:
            await message.answer("Устройство не найдено: /amnezia.")
            return
        await message.answer(
            f"Удалить ключ «{html_escape(str(target['name']))}»? Подключение "
            "AmneziaWG на этом устройстве сразу перестанет работать.",
            reply_markup=delete_confirm_kb(device_id),
        )
    elif action == "delyes" and device_id is not None:
        try:
            removed = await awg.remove_device(owner_id, device_id)
        except AwgError:
            await message.answer(UNAVAILABLE)
            return
        await message.edit_reply_markup(reply_markup=None)
        await message.answer(
            "🗑 Ключ удалён." if removed else "Устройство не найдено: /amnezia."
        )
        await show_menu(message, owner_id, services)
