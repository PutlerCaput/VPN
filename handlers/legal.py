"""Public offer: /legal and the menu button."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from handlers.common import DISCLAIMER
from keyboards.main_menu import BTN_LEGAL
from services.container import Services

router = Router(name="legal")
OFFER_PATH = Path(__file__).resolve().parent.parent / "legal" / "oferta.md"


@lru_cache(maxsize=1)
def offer_text() -> str:
    return OFFER_PATH.read_text(encoding="utf-8")


async def _send_offer(message: Message, offer_url: str) -> None:
    await message.answer_document(
        BufferedInputFile(offer_text().encode("utf-8"), filename="oferta.md"),
        caption=f"📄 Публичная оферта. Онлайн-версия: {offer_url}\n\n{DISCLAIMER}",
    )


@router.message(F.text == BTN_LEGAL)
@router.message(Command("legal"))
async def cmd_legal(message: Message, services: Services) -> None:
    await _send_offer(message, services.settings.offer_url)


@router.callback_query(F.data == "offer:full")
async def cb_full_offer(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _send_offer(callback.message, services.settings.offer_url)
