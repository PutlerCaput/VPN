"""Brand animation: caption limit, file_id cache, and plain-text fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Chat, FSInputFile, Message, User

import services.brand_animation as brand
from services.brand_animation import (
    ANIMATION_PATH,
    CAPTION_LIMIT,
    fits_caption,
    send_branded,
    visible_length,
)
from services.faq import ABOUT_ANSWER

OWNER = 995101


def _message() -> Message:
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER, type="private"),
        from_user=User(id=OWNER, is_bot=False, first_name="Owner"),
    )
    object.__setattr__(message, "answer", AsyncMock())
    object.__setattr__(message, "answer_photo", AsyncMock())
    object.__setattr__(message, "answer_animation", AsyncMock())
    return message


def _uploaded(file_id: str = "anim-file") -> SimpleNamespace:
    return SimpleNamespace(animation=SimpleNamespace(file_id=file_id))


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    brand.clear_file_id_cache()
    yield
    brand.clear_file_id_cache()


def test_visible_length_counts_characters_after_html_is_parsed() -> None:
    assert visible_length("<b>Привет</b>") == 6
    assert visible_length("A &amp; B") == 5
    assert fits_caption("a" * CAPTION_LIMIT)
    assert not fits_caption("a" * (CAPTION_LIMIT + 1))
    assert fits_caption("<b>" + "a" * CAPTION_LIMIT + "</b>")
    assert visible_length(ABOUT_ANSWER) <= CAPTION_LIMIT


def test_animation_file_is_a_small_mpeg4_without_an_audio_track() -> None:
    data = ANIMATION_PATH.read_bytes()
    gif = ANIMATION_PATH.with_suffix(".gif")
    assert ANIMATION_PATH.is_file()
    assert gif.is_file()
    # ISO BMFF: an `ftyp` box near the start (faststart puts moov early too).
    assert data[4:8] == b"ftyp"
    assert b"mp4" in data[8:32] or b"isom" in data[8:32] or b"avc1" in data[8:32]
    assert b"mp4a" not in data  # no AAC audio track
    assert len(data) < 600_000
    assert gif.stat().st_size > 0


async def test_short_text_travels_as_the_animation_caption() -> None:
    message = _message()
    message.answer_animation.return_value = _uploaded()
    text = "👋 <b>Добро пожаловать</b>"
    sent = await send_branded(text, enabled=True, reply_markup="kb", message=message)
    assert sent is True
    message.answer_animation.assert_awaited_once()
    args, kwargs = message.answer_animation.await_args
    assert isinstance(args[0], FSInputFile)
    assert kwargs["caption"] == text
    assert kwargs["reply_markup"] == "kb"
    message.answer.assert_not_awaited()
    assert brand.cached_animation_file_id() == "anim-file"


async def test_long_text_is_sent_after_the_animation() -> None:
    message = _message()
    message.answer_animation.return_value = _uploaded()
    text = "x" * (CAPTION_LIMIT + 1)
    await send_branded(text, enabled=True, reply_markup="kb", message=message)
    args, kwargs = message.answer_animation.await_args
    assert kwargs["caption"] is None
    assert kwargs["reply_markup"] is None
    message.answer.assert_awaited_once_with(text, reply_markup="kb")


async def test_file_id_is_reused_after_the_first_upload() -> None:
    first = _message()
    first.answer_animation.return_value = _uploaded("cached-id")
    await send_branded("один", enabled=True, message=first)
    second = _message()
    second.answer_animation.return_value = _uploaded("ignored")
    await send_branded("два", enabled=True, message=second)
    assert second.answer_animation.await_args.args[0] == "cached-id"
    assert brand.cached_animation_file_id() == "cached-id"


async def test_animation_failure_falls_back_to_plain_text() -> None:
    message = _message()
    message.answer_animation.side_effect = TelegramNetworkError(
        method=MagicMock(), message="down"
    )
    text = "оферта"
    sent = await send_branded(text, enabled=True, reply_markup="kb", message=message)
    assert sent is False
    message.answer.assert_awaited_once_with(text, reply_markup="kb")
    assert brand.cached_animation_file_id() is None


async def test_animation_failure_on_start_still_tries_the_old_photo() -> None:
    message = _message()
    message.answer_animation.side_effect = TelegramNetworkError(
        method=MagicMock(), message="down"
    )
    message.answer_photo.return_value = SimpleNamespace(
        photo=[SimpleNamespace(file_id="still")]
    )
    await send_branded("оферта", enabled=True, message=message, still_fallback=True)
    message.answer_photo.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert brand.cached_welcome_photo_file_id() == "still"


async def test_disabled_flag_skips_the_animation() -> None:
    message = _message()
    await send_branded("текст", enabled=False, reply_markup="kb", message=message)
    message.answer_animation.assert_not_awaited()
    message.answer_photo.assert_not_awaited()
    message.answer.assert_awaited_once_with("текст", reply_markup="kb")


async def test_disabled_flag_keeps_the_old_still_for_new_users() -> None:
    message = _message()
    message.answer_photo.return_value = SimpleNamespace(
        photo=[SimpleNamespace(file_id="still")]
    )
    await send_branded("оферта", enabled=False, message=message, still_fallback=True)
    message.answer_animation.assert_not_awaited()
    message.answer_photo.assert_awaited_once()
    message.answer.assert_awaited_once()


async def test_chat_id_path_used_by_payment_notifications() -> None:
    bot = SimpleNamespace(
        send_animation=AsyncMock(return_value=_uploaded("pay-id")),
        send_message=AsyncMock(),
    )
    await send_branded(
        "✅ оплата получена",
        enabled=True,
        bot=bot,  # type: ignore[arg-type]
        chat_id=OWNER,
        disable_web_page_preview=True,
    )
    bot.send_animation.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert brand.cached_animation_file_id() == "pay-id"
