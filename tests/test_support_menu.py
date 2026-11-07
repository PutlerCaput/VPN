"""Support button: FAQ topics first, the person one button away."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from handlers.support import (
    SUPPORT_MENU_TEXT,
    SupportState,
    support_callback,
    support_start,
)


async def test_support_button_shows_faq_topics_before_a_person() -> None:
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    state = MagicMock(spec=FSMContext)
    state.clear = AsyncMock()
    state.set_state = AsyncMock()

    await support_start(message, state)

    state.clear.assert_awaited_once()
    state.set_state.assert_not_awaited()
    args, kwargs = message.answer.await_args
    assert args[0] == SUPPORT_MENU_TEXT
    callbacks = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "faq:tariff" in callbacks
    assert callbacks[-1] == "support:write"


async def test_write_button_hands_the_next_message_to_a_person() -> None:
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    message.chat = SimpleNamespace(type="private", id=42)
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = SimpleNamespace(id=42)
    callback.answer = AsyncMock()
    state = MagicMock(spec=FSMContext)
    state.set_state = AsyncMock()

    await support_callback(callback, state)

    state.set_state.assert_awaited_once_with(SupportState.waiting)
    message.answer.assert_awaited_once()


def test_menu_text_names_the_person_button() -> None:
    assert "«✍️ Написать человеку»" in SUPPORT_MENU_TEXT
