"""The last word when a bot handler fails: the user gets an answer, not silence.

Without this an exception in a handler (most often the database gateway on the
other server not answering for a moment) was only logged by aiogram, and the
person who pressed the button saw nothing at all.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ErrorEvent, Message

from db.remote import DatabaseUnavailableError

logger = logging.getLogger(__name__)

UNAVAILABLE_TEXT = "⏳ Сервис временно недоступен. Повторите, пожалуйста, через минуту."
# Telegram has taken the money when this message arrives, so "try again" would
# invite a second payment: the user is told it arrived and where to ask.
PAYMENT_DELAYED_TEXT = (
    "✅ Оплата получена, но её подтверждение задержалось из-за кратковременного "
    "сбоя. Если подписка не продлится в течение 15 минут, напишите /paysupport."
)
FAILED_TEXT = "⚠️ Что-то пошло не так. Попробуйте ещё раз или напишите в «💬 Поддержка»."


def _cause_name(exc: BaseException) -> str:
    cause = exc.__cause__ or exc.__context__
    return type(cause).__name__ if cause is not None else "none"


async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    update = event.update
    paid = isinstance(update.message, Message) and (
        getattr(update.message, "successful_payment", None) is not None
    )
    unavailable = isinstance(exc, DatabaseUnavailableError)
    if paid:
        # A payment that was not recorded: always an error line for the owner.
        logger.error(
            "bot_payment_handler_failed",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"failure": type(exc).__name__},
        )
    elif unavailable:
        # Expected during a short outage between the servers: no traceback.
        logger.warning(
            "bot_handler_database_unavailable",
            extra={"failure": _cause_name(exc)},
        )
    else:
        logger.error(
            "bot_handler_failed",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"failure": type(exc).__name__},
        )
    if paid:
        text = PAYMENT_DELAYED_TEXT
    else:
        text = UNAVAILABLE_TEXT if unavailable else FAILED_TEXT
    # The answer itself may fail (the user blocked the bot, the query expired,
    # Telegram is unreachable): that must not raise a second error.
    query = update.callback_query
    message = update.message
    if isinstance(query, CallbackQuery):
        try:
            await query.answer(text, show_alert=True)
            return True
        except (TelegramAPIError, OSError):
            # Already answered by the handler before it failed: say it in the chat.
            message = query.message if isinstance(query.message, Message) else None
    if isinstance(message, Message):
        with contextlib.suppress(TelegramAPIError, OSError):
            await message.answer(text)
    return True


def register_error_handler(dp: Dispatcher) -> None:
    dp.errors.register(on_error)
