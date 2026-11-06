"""Owner monitoring is private and must not allow state changes from Telegram."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from handlers.admin import router


async def test_read_only_owner_router_blocks_management_and_nonowners(
    services: Any,
) -> None:
    bot = Bot(token=services.settings.bot_token.get_secret_value())
    session = AsyncMock()
    bot.session = session
    dp = Dispatcher()
    dp["services"] = services
    dp.include_router(router)
    services.provisioning.process_paid_payment = AsyncMock()
    services.provisioning.autoban = AsyncMock()
    services.provisioning.unsuspend = AsyncMock()
    services.db.get_user = AsyncMock()
    services.db.get_support_target = AsyncMock()

    async def send(text: str, sender: int, chat_type: str = "private") -> None:
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=sender, type=chat_type),
            from_user=User(id=sender, is_bot=False, first_name="Owner"),
            text=text,
        )
        await dp.feed_update(bot, Update(update_id=1, message=message))

    try:
        await send("/status", services.settings.admin_telegram_id)
        reply = session.call_args.args[1]
        assert "БД: доступна" in reply.text
        assert "этой командой не проверяется" in reply.text
        for command in (
            "/confirm " + "a" * 32,
            "/ban 123456 CYBERCRIME",
            "/unsuspend 123456",
            "/user 123456",
        ):
            session.reset_mock()
            await send(command, services.settings.admin_telegram_id)
            assert "Операции управления отключены" in session.call_args.args[1].text
        services.provisioning.process_paid_payment.assert_not_awaited()
        services.provisioning.autoban.assert_not_awaited()
        services.provisioning.unsuspend.assert_not_awaited()
        services.db.get_user.assert_not_awaited()
        services.db.get_support_target.assert_not_awaited()
        session.reset_mock()
        await send("/status", services.settings.admin_telegram_id + 1)
        await send("/status", services.settings.admin_telegram_id, "group")
        session.assert_not_called()
        services.db.ping = AsyncMock(side_effect=RuntimeError("sensitive error text"))
        await send("/status", services.settings.admin_telegram_id)
        reply = session.call_args.args[1]
        assert "ошибка проверки" in reply.text
        assert "sensitive error text" not in reply.text
    finally:
        await dp.storage.close()
        await bot.session.close()
        # Router is module-global; leave it reusable by other dispatcher tests.
        dp.sub_routers.remove(router)
        router._parent_router = None
