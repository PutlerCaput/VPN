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
from services.brand_animation import send_branded
from services.container import Services
from services.faq import NOT_FOUND_TEXT, TOPICS_BY_SLUG, Topic, match_topic

router = Router(name="faq")


async def _send_topic(message: Message, topic: Topic, services: Services) -> None:
    markup = faq_topics_kb()
    if topic.slug == "about":
        await send_branded(
            topic.answer,
            enabled=services.settings.brand_animation_enabled,
            reply_markup=markup,
            message=message,
        )
        return
    await message.answer(topic.answer, reply_markup=markup)


@router.message(StateFilter(default_state), F.text, ~F.text.startswith("/"))
async def faq_question(message: Message, services: Services) -> None:
    topic = match_topic(message.text or "")
    if topic is None:
        await message.answer(NOT_FOUND_TEXT, reply_markup=faq_topics_kb())
        return
    await _send_topic(message, topic, services)


@router.callback_query(F.data.startswith(FAQ_PREFIX))
async def faq_topic(callback: CallbackQuery, services: Services) -> None:
    topic = TOPICS_BY_SLUG.get((callback.data or "").removeprefix(FAQ_PREFIX))
    reply_to = callback.message
    if topic is None or not isinstance(reply_to, Message):
        await callback.answer("Выберите тему заново.")
        return
    await callback.answer()
    await _send_topic(reply_to, topic, services)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Не понял сообщение 🙂 Выберите действие в меню ниже или напишите "
        "в «💬 Поддержка».",
        reply_markup=main_menu_kb(),
    )
