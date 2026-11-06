"""User-initiated setup and self-help. No traffic histories or tracking pixels."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from handlers.profile import deliver_config
from keyboards.main_menu import BTN_SETUP
from keyboards.onboarding import guide_kb, help_kb, setup_kb
from services.connection_guide import (
    AMNEZIA_GUIDE,
    GUIDES,
    HELP_TEXT,
    KILL_SWITCH_TEXT,
)
from services.container import Services

router = Router(name="onboarding")
SETUP_TEXT = (
    "📱 <b>Подключение устройства</b>\n\n"
    "Выберите систему. Бот покажет установку клиента, импорт профиля "
    "и проверку подключения. Для получения конфигурации нужна активная подписка "
    "или пробный доступ после /start."
)


@router.message(F.text == BTN_SETUP)
@router.message(Command("connect"))
async def start_setup(
    message: Message, state: FSMContext, services: Services | None = None
) -> None:
    await state.clear()
    awg = services is not None and services.awg is not None
    await message.answer(SETUP_TEXT, reply_markup=setup_kb(awg))


@router.message(Command("help"))
async def show_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=help_kb())


@router.callback_query(F.data.startswith("setup:"))
async def setup_action(
    callback: CallbackQuery, state: FSMContext, services: Services
) -> None:
    message = callback.message
    if (
        not isinstance(message, Message)
        or message.chat.type != "private"
        or message.chat.id != callback.from_user.id
    ):
        await callback.answer("Откройте личный чат с ботом.")
        return
    action = callback.data or ""
    if action.startswith("setup:guide:"):
        key = action.removeprefix("setup:guide:")
        guide = (
            AMNEZIA_GUIDE
            if key == "amnezia" and services.awg is not None
            else GUIDES.get(key)
        )
        if guide is None:
            await callback.answer("Выберите устройство заново: /connect.")
            return
        await callback.answer()
        await state.clear()
        await message.answer(
            f"📱 <b>{guide.title} · {guide.client}</b>\n\n{guide.steps}\n\n"
            "На одном устройстве удобнее импорт по ссылке. "
            "QR-код можно сканировать с другого экрана. "
            "Ссылку и QR-код никому не передавайте.",
            reply_markup=guide_kb(key),
        )
    elif action == "setup:choose":
        await callback.answer()
        await start_setup(message, state, services)
    elif action == "setup:killswitch":
        await callback.answer()
        await message.answer(KILL_SWITCH_TEXT, reply_markup=help_kb())
    elif action == "setup:help":
        await callback.answer()
        await show_help(message, state)
    elif action == "setup:config":
        await callback.answer()
        await state.clear()
        await deliver_config(message, callback.from_user.id, services)
    else:
        await callback.answer("Инструкция обновлена. Нажмите /connect.")
