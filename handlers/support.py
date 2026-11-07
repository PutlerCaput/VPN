"""Support relay: user -> admin (copy), admin reply -> user."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
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
        "💬 <b>Поддержка</b>\n\n"
        "Опишите вопрос одним сообщением — ответит живой человек, прямо в этот чат.\n\n"
        "Чтобы решить быстрее, укажите:\n"
        "• для подключения — устройство, приложение, сеть и текст ошибки;\n"
        "• для оплаты — номер заказа и время.\n\n"
        "Скриншот можно приложить, закрыв личные данные. Не присылайте ссылку "
        "подключения, QR-код, пароли и ключи.\n\n"
        "Частые вопросы — /help. Отмена — /cancel.",
    )


PAYSUPPORT_TEXT = (
    "🧾 <b>Вопрос по оплате</b>\n\n"
    "Опишите проблему одним сообщением: номер заказа (его бот присылает вместе "
    "со счётом), способ оплаты, сумму и примерное время. Для Telegram Stars "
    "можно также переслать сообщение об успешной оплате. Ответ придёт в этот "
    "чат. Возврат звёзд оформляется через бота по решению поддержки.\n\n"
    "Не присылайте данные карты, пароли и коды из SMS.\nОтмена: /cancel"
)


async def forward_to_admin(
    message: Message, services: Services, *, topic: str | None = None
) -> bool:
    """Copy a user's message to the owner. False if Telegram refused it."""
    if message.from_user is None:
        return False
    admin_id = services.settings.admin_telegram_id
    user = message.from_user
    row = await services.db.get_user(user.id)
    tier = TIERS.get(str(row["tier"])) if row and row["status"] == "active" else None
    priority = (
        f"⭐ <b>Приоритет</b> ({tier.title})\n"
        if tier and tier.priority_support
        else ""
    )
    subject = "🧾 <b>Вопрос по оплате</b>\n" if topic == "payment" else ""
    header = (
        f"{priority}{subject}📨 Обращение от <code>{user.id}</code> "
        f"@{html_escape(user.username or '—')}\nОтветьте реплаем на копию ниже."
    )
    try:
        await services.bot.send_message(admin_id, header)
        copied = await message.copy_to(admin_id)
    except TelegramAPIError:
        logger.exception("support_forward_failed")
        return False
    await services.db.add_support_message(copied.message_id, user.id)
    return True


@router.message(Command("paysupport"))
async def paysupport(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    services: Services,
    db_user: dict[str, object] | None = None,
) -> None:
    """Required by Telegram for bots that accept payments."""
    if command.args and command.args.strip():
        if await forward_to_admin(message, services, topic="payment"):
            await message.answer("✅ Вопрос по оплате отправлен. Ответ придёт сюда.")
        else:
            await message.answer("⚠️ Не удалось отправить. Повторите позже.")
        return
    if db_user is not None and db_user.get("status") == "banned":
        await message.answer(
            "Напишите вопрос по оплате одним сообщением после команды, например:\n"
            "<code>/paysupport заказ 0123abcd, оплата не зачислена</code>"
        )
        return
    await state.set_state(SupportState.waiting)
    await state.update_data(topic="payment")
    await message.answer(PAYSUPPORT_TEXT)


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
    data = await state.get_data()
    topic = data.get("topic") if isinstance(data, dict) else None
    if not await forward_to_admin(
        message, services, topic=topic if isinstance(topic, str) else None
    ):
        await message.answer(
            "⚠️ Не удалось отправить сообщение. Отправьте его повторно позже "
            "или нажмите /cancel."
        )
        return
    await state.clear()
    await message.answer(
        "✅ Сообщение получено. Ответим в этот чат, как только разберёмся.",
        reply_markup=main_menu_kb(),
    )
