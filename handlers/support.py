"""Support relay: user -> admin (copy), admin reply -> user."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import TIERS
from keyboards.main_menu import BTN_SUPPORT, main_menu_kb
from services.container import Services
from services.security import html_escape

logger = logging.getLogger(__name__)
router = Router(name="support")


class SupportState(StatesGroup):
    waiting = State()


@router.message(F.text == BTN_SUPPORT)
@router.message(Command("support"))
async def support_start(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting)
    await message.answer(
        "💬 Опишите вопрос одним сообщением. Можно приложить скриншот "
        "с закрытыми персональными данными. Для подключения укажите систему, "
        "клиент, сеть и текст ошибки; для оплаты — номер заказа и время. "
        "Не присылайте ссылку подключения, QR-код, пароли и ключи.\n\n"
        "Сначала можно проверить /help. Ответ придёт в этот чат; "
        "мгновенный ответ не гарантируется.\nОтмена: /cancel",
    )


@router.callback_query(F.data == "support:write")
async def support_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if (
        not isinstance(callback.message, Message)
        or callback.message.chat.type != "private"
        or callback.message.chat.id != callback.from_user.id
    ):
        await callback.answer("Откройте личный чат с ботом.")
        return
    await callback.answer()
    await support_start(callback.message, state)


@router.message(Command("cancel"), StateFilter(SupportState.waiting))
async def support_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


@router.message(StateFilter(SupportState.waiting))
async def support_forward(
    message: Message, state: FSMContext, services: Services
) -> None:
    if message.from_user is None:
        return
    admin_id = services.settings.admin_telegram_id
    user = message.from_user
    row = await services.db.get_user(user.id)
    tier = TIERS.get(str(row["tier"])) if row and row["status"] == "active" else None
    priority = (
        f"⭐ <b>Приоритет</b> ({tier.title})\n"
        if tier and tier.priority_support
        else ""
    )
    header = (
        f"{priority}📨 Обращение от <code>{user.id}</code> "
        f"@{html_escape(user.username or '—')}\nОтветьте реплаем на копию ниже."
    )
    try:
        await services.bot.send_message(admin_id, header)
        copied = await message.copy_to(admin_id)
    except TelegramAPIError:
        logger.exception("support_forward_failed")
        await message.answer(
            "⚠️ Не удалось отправить сообщение. Отправьте его повторно позже "
            "или нажмите /cancel."
        )
        return
    await services.db.add_support_message(copied.message_id, user.id)
    await state.clear()
    await message.answer(
        "✅ Сообщение отправлено. Ответ придёт сюда.", reply_markup=main_menu_kb()
    )


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите действие в меню ниже.", reply_markup=main_menu_kb())
