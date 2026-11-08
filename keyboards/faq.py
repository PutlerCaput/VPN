"""Buttons under the customer FAQ answers."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.faq import TOPICS

FAQ_PREFIX = "faq:"
_PER_ROW = 2


def faq_topics_kb() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=topic.label,
            callback_data=f"{FAQ_PREFIX}{topic.slug}",
        )
        for topic in TOPICS
        if topic.in_menu
    ]
    rows = [buttons[i : i + _PER_ROW] for i in range(0, len(buttons), _PER_ROW)]
    rows.append(
        [
            InlineKeyboardButton(
                text="✍️ Написать человеку", callback_data="support:write"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
