"""Customer questions in private chat: answer by topic, otherwise point to support.

The router is included after the support flow, so a message sent while support
waits for the owner's answer still goes to the owner. The catch-all fallback
moved here from support.py and stays last.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

from keyboards.faq import FAQ_PREFIX, faq_topics_kb
from keyboards.main_menu import main_menu_kb
from services.faq import NOT_FOUND_TEXT, TOPICS_BY_SLUG, match_topic

router = Router(name="faq")


@router.message(StateFilter(default_state), F.text, ~F.text.startswith("/"))
async def faq_question(message: Message) -> None:
    topic = match_topic(message.text or "")
    if topic is None:
        await message.answer(NOT_FOUND_TEXT, reply_markup=faq_topics_kb())
        return
    await message.answer(topic.answer, reply_markup=faq_topics_kb())


@router.callback_query(F.data.startswith(FAQ_PREFIX))
async def faq_topic(callback: CallbackQuery) -> None:
    topic = TOPICS_BY_SLUG.get((callback.data or "").removeprefix(FAQ_PREFIX))
    reply_to = callback.message
    if topic is None or not isinstance(reply_to, Message):
        await callback.answer("Выберите тему заново.")
        return
    await callback.answer()
    await reply_to.answer(topic.answer, reply_markup=faq_topics_kb())


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Не понял сообщение 🙂 Выберите действие в меню ниже или напишите "
        "в «💬 Поддержка».",
        reply_markup=main_menu_kb(),
    )
