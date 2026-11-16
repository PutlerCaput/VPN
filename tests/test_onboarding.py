"""Exercise credential ownership, private delivery and retryable support."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import CopyMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from handlers import build_root_router
from handlers.common import send_config
from handlers.onboarding import setup_action
from handlers.profile import deliver_config
from handlers.start import cmd_start
from handlers.support import support_cancel, support_forward


def message_for(owner: int, *, chat_type: str = "private") -> Message:
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=owner, type=chat_type),
        from_user=User(id=999, is_bot=True, first_name="Bot"),
    )
    for name in ("answer", "answer_photo", "answer_animation", "copy_to"):
        object.__setattr__(message, name, AsyncMock())
    return message


async def test_setup_callback_delivers_only_to_its_owner(services: Any) -> None:
    await services.db.create_user(123456, "owner", None)
    await services.provisioning.accept_offer_and_start_trial(123456)
    message = message_for(123456)
    callback = CallbackQuery(
        id="callback",
        chat_instance="instance",
        data="setup:config",
        from_user=User(id=123456, is_bot=False, first_name="Owner"),
        message=message,
    )
    object.__setattr__(callback, "answer", AsyncMock())
    state = AsyncMock()
    await setup_action(callback, state, services)
    assert "vless://" in message.answer.call_args.args[0]
    message.answer_photo.assert_awaited_once()
    message.answer.reset_mock()
    message.answer_photo.reset_mock()
    # A malformed/stale callback must not deliver another account's credentials.
    object.__setattr__(
        callback, "from_user", User(id=234567, is_bot=False, first_name="Other")
    )
    await setup_action(callback, state, services)
    message.answer.assert_not_awaited()
    message.answer_photo.assert_not_awaited()


@pytest.mark.parametrize("status", ["pending", "expired", "suspended", "banned"])
async def test_config_requires_active_and_accepted(services: Any, status: str) -> None:
    await services.db.create_user(123456, "owner", None)
    await services.db.accept_offer(123456)
    await services.db.set_user_status(123456, status)
    services.provisioning.fresh_config = AsyncMock()
    message = message_for(123456)
    await deliver_config(message, 123456, services)
    services.provisioning.fresh_config.assert_not_awaited()
    message.answer_photo.assert_not_awaited()


async def test_config_without_offer_is_denied(services: Any) -> None:
    await services.db.create_user(123456, "owner", None)
    await services.db.set_user_status(123456, "active")
    services.provisioning.fresh_config = AsyncMock()
    message = message_for(123456)
    await deliver_config(message, 123456, services)
    services.provisioning.fresh_config.assert_not_awaited()
    assert "оферту" in message.answer.call_args.args[0]


async def test_config_html_safe_and_not_sent_to_groups() -> None:
    link = "vless://example?security=reality&type=tcp#<name>"
    message = message_for(123456)
    await send_config(message, link, None)
    assert "&amp;type=tcp#&lt;name&gt;" in message.answer.call_args.args[0]
    group = message_for(-123456, chat_type="group")
    await send_config(group, link, None)
    group.answer.assert_not_awaited()
    group.answer_photo.assert_not_awaited()


async def test_root_does_not_handle_group_credentials(services: Any) -> None:
    bot = Bot(token=services.settings.bot_token.get_secret_value())
    session = AsyncMock()
    bot.session = session
    root = build_root_router()
    dp = Dispatcher()
    dp["services"] = services
    dp.include_router(root)
    services.db.get_user = AsyncMock()
    try:
        for chat_type in ("group", "supergroup"):
            message = message_for(-123456, chat_type=chat_type)
            object.__setattr__(message, "text", "/config")
            await dp.feed_update(bot, Update(update_id=1, message=message))
            callback = CallbackQuery(
                id="callback",
                chat_instance="instance",
                data="offer:accept",
                from_user=User(id=123456, is_bot=False, first_name="Owner"),
                message=message,
            )
            await dp.feed_update(bot, Update(update_id=2, callback_query=callback))
        services.db.get_user.assert_not_awaited()
        session.assert_not_called()
    finally:
        for child in list(root.sub_routers):
            root.sub_routers.remove(child)
            child._parent_router = None
        await dp.storage.close()
        await bot.session.close()


async def test_failed_support_delivery_can_be_retried(services: Any) -> None:
    message = message_for(123456)
    object.__setattr__(
        message, "from_user", User(id=123456, is_bot=False, first_name="Owner")
    )
    state = AsyncMock()
    services.bot.send_message = AsyncMock()
    message.copy_to.side_effect = TelegramNetworkError(
        method=CopyMessage(chat_id=999, from_chat_id=123456, message_id=10),
        message="network failed",
    )
    await support_forward(message, state, services)
    state.clear.assert_not_awaited()
    message.copy_to.side_effect = None
    message.copy_to.return_value = SimpleNamespace(message_id=991)
    await support_forward(message, state, services)
    state.clear.assert_awaited_once()
    assert await services.db.get_support_target(991) == 123456
    await support_cancel(message, state)
    assert state.clear.await_count == 2


async def test_registration_ignores_new_referrals_when_disabled(services: Any) -> None:
    from aiogram.filters import CommandObject

    await services.db.create_user(234567, "inviter", None)
    message = message_for(123456)
    object.__setattr__(
        message, "from_user", User(id=123456, is_bot=False, first_name="Owner")
    )
    await cmd_start(message, CommandObject(command="start", args="234567"), services)
    assert (await services.db.get_user(123456))["referred_by"] is None


async def test_unknown_guide_does_not_fetch_credentials(services: Any) -> None:
    message = message_for(123456)
    callback = CallbackQuery(
        id="callback",
        chat_instance="instance",
        data="setup:guide:unrecognized",
        from_user=User(id=123456, is_bot=False, first_name="Owner"),
        message=message,
    )
    object.__setattr__(callback, "answer", AsyncMock())
    services.provisioning.fresh_config = AsyncMock()
    await setup_action(callback, AsyncMock(), services)
    services.provisioning.fresh_config.assert_not_awaited()
    message.answer.assert_not_awaited()
