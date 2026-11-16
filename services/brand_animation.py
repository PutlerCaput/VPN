"""Brand animation for a few Telegram entry points (not every menu step)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, Message

logger = logging.getLogger(__name__)

# Telegram counts caption length after parsing entities, not raw HTML.
CAPTION_LIMIT = 1024

ANIMATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "branding"
    / "telegram"
    / "brand-animation.mp4"
)
WELCOME_IMAGE = (
    Path(__file__).resolve().parent.parent
    / "branding"
    / "telegram"
    / "welcome-1280x720.jpg"
)

# Telegram file_id after the first successful upload, so later sends do not
# re-upload the MP4. Process-local: a restart costs one upload.
_file_ids: dict[str, str] = {}

_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z]+);")


def visible_length(text: str) -> int:
    """Characters Telegram counts after HTML entities are parsed."""
    return len(_ENTITY.sub("x", _TAG.sub("", text)))


def fits_caption(text: str, *, limit: int = CAPTION_LIMIT) -> bool:
    return visible_length(text) <= limit


def cached_animation_file_id() -> str | None:
    return _file_ids.get("animation")


def cached_welcome_photo_file_id() -> str | None:
    return _file_ids.get("photo")


def clear_file_id_cache() -> None:
    _file_ids.clear()


def _remember_animation(sent: Message) -> None:
    animation = getattr(sent, "animation", None)
    file_id = getattr(animation, "file_id", None)
    if isinstance(file_id, str) and file_id:
        _file_ids.setdefault("animation", file_id)


def _remember_photo(sent: Message) -> None:
    sizes = getattr(sent, "photo", None)
    if sizes and isinstance(getattr(sizes[-1], "file_id", None), str):
        _file_ids.setdefault("photo", sizes[-1].file_id)


def _animation_source() -> str | FSInputFile | None:
    file_id = _file_ids.get("animation")
    if file_id:
        return file_id
    if ANIMATION_PATH.is_file():
        return FSInputFile(ANIMATION_PATH)
    return None


def _photo_source() -> str | FSInputFile | None:
    file_id = _file_ids.get("photo")
    if file_id:
        return file_id
    if WELCOME_IMAGE.is_file():
        return FSInputFile(WELCOME_IMAGE)
    return None


async def _answer_animation(
    *,
    animation: str | FSInputFile,
    caption: str | None,
    reply_markup: Any,
    message: Message | None,
    bot: Bot | None,
    chat_id: int | None,
) -> Message:
    if message is not None:
        return await message.answer_animation(
            animation, caption=caption, reply_markup=reply_markup
        )
    if bot is None or chat_id is None:
        raise ValueError("bot and chat_id are required when message is omitted")
    return await bot.send_animation(
        chat_id, animation, caption=caption, reply_markup=reply_markup
    )


async def _answer_text(
    *,
    text: str,
    reply_markup: Any,
    message: Message | None,
    bot: Bot | None,
    chat_id: int | None,
    disable_web_page_preview: bool,
) -> Message:
    if message is not None:
        return await message.answer(text, reply_markup=reply_markup)
    if bot is None or chat_id is None:
        raise ValueError("bot and chat_id are required when message is omitted")
    return await bot.send_message(
        chat_id,
        text,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_web_page_preview,
    )


async def _answer_photo(
    *,
    photo: str | FSInputFile,
    message: Message,
) -> Message:
    return await message.answer_photo(photo)


async def send_welcome_still(message: Message) -> None:
    """The previous still logo. A missing file or an upload error is ignored."""
    photo = _photo_source()
    if photo is None:
        return
    try:
        sent = await _answer_photo(photo=photo, message=message)
    except TelegramAPIError:
        logger.warning("welcome_image_failed")
        return
    _remember_photo(sent)


async def send_branded(
    text: str,
    *,
    enabled: bool,
    reply_markup: Any = None,
    message: Message | None = None,
    bot: Bot | None = None,
    chat_id: int | None = None,
    disable_web_page_preview: bool = False,
    still_fallback: bool = False,
) -> bool:
    """Send the brand animation with ``text`` when it is appropriate.

    If the text fits a caption, it travels with the animation and the keyboard.
    Otherwise the animation is sent first and the text follows in a separate
    message (keyboards stay on the text). Any animation failure falls back to
    plain text, and optionally to the old welcome still image first.

    Returns True when the animation was delivered.
    """
    if not enabled:
        if still_fallback and message is not None:
            await send_welcome_still(message)
        await _answer_text(
            text=text,
            reply_markup=reply_markup,
            message=message,
            bot=bot,
            chat_id=chat_id,
            disable_web_page_preview=disable_web_page_preview,
        )
        return False

    animation = _animation_source()
    if animation is None:
        if still_fallback and message is not None:
            await send_welcome_still(message)
        await _answer_text(
            text=text,
            reply_markup=reply_markup,
            message=message,
            bot=bot,
            chat_id=chat_id,
            disable_web_page_preview=disable_web_page_preview,
        )
        return False

    use_caption = fits_caption(text)
    try:
        sent = await _answer_animation(
            animation=animation,
            caption=text if use_caption else None,
            reply_markup=reply_markup if use_caption else None,
            message=message,
            bot=bot,
            chat_id=chat_id,
        )
    except TelegramAPIError:
        logger.warning("brand_animation_failed")
        if still_fallback and message is not None:
            await send_welcome_still(message)
        await _answer_text(
            text=text,
            reply_markup=reply_markup,
            message=message,
            bot=bot,
            chat_id=chat_id,
            disable_web_page_preview=disable_web_page_preview,
        )
        return False

    _remember_animation(sent)
    if not use_caption:
        await _answer_text(
            text=text,
            reply_markup=reply_markup,
            message=message,
            bot=bot,
            chat_id=chat_id,
            disable_web_page_preview=disable_web_page_preview,
        )
    return True
