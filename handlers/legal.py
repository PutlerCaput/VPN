"""Public offer and privacy policy: /legal, /terms, /privacy and the menu button."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers.common import DISCLAIMER
from keyboards.main_menu import BTN_LEGAL
from services.container import Services

router = Router(name="legal")
OFFER_PATH = Path(__file__).resolve().parent.parent / "legal" / "oferta.md"
PRIVACY_PATH = OFFER_PATH.with_name("privacy.md")


@lru_cache(maxsize=1)
def offer_text() -> str:
    return OFFER_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def privacy_text() -> str:
    return PRIVACY_PATH.read_text(encoding="utf-8")


def legal_links_kb(services: Services) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 Оферта на сайте", url=services.settings.offer_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Политика обработки персональных данных",
                    callback_data="privacy:full",
                )
            ],
        ]
    )


async def _send_privacy(message: Message, privacy_url: str) -> None:
    await message.answer_document(
        BufferedInputFile(privacy_text().encode("utf-8"), filename="privacy.md"),
        caption=(
            "🔒 Политика обработки персональных данных. "
            f"Онлайн-версия: {privacy_url}\n\n"
            "Узнать, какие ваши данные хранятся, исправить или удалить их: "
            "напишите запрос в «💬 Поддержка». Ответим в течение 10 рабочих дней."
        ),
    )


async def _send_offer(message: Message, offer_url: str) -> None:
    await message.answer_document(
        BufferedInputFile(offer_text().encode("utf-8"), filename="oferta.md"),
        caption=f"📄 Публичная оферта. Онлайн-версия: {offer_url}\n\n{DISCLAIMER}",
    )


@router.message(F.text == BTN_LEGAL)
@router.message(Command("legal"))
@router.message(Command("terms"))
async def cmd_legal(message: Message, services: Services) -> None:
    await _send_offer(message, services.settings.offer_url)
    await message.answer("Документы сервиса:", reply_markup=legal_links_kb(services))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, services: Services) -> None:
    await _send_privacy(message, services.settings.privacy_url)


@router.callback_query(F.data == "privacy:full")
async def cb_privacy(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _send_privacy(callback.message, services.settings.privacy_url)


@router.callback_query(F.data == "offer:full")
async def cb_full_offer(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _send_offer(callback.message, services.settings.offer_url)
