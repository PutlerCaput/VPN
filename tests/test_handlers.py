from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Chat, Message, User

from handlers.payments import is_usdt_payment_request
from handlers.start import parse_start_arg, welcome_message
from middlewares.banned import BannedMiddleware, is_allowed_for_banned
from middlewares.throttling import ThrottlingMiddleware
from services.security import SlidingWindowLimiter

USER = User(id=100500, is_bot=False, first_name="Test")


def make_message(text: str) -> Message:
    message = Message.model_construct(
        message_id=1,
        date=0,
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        text=text,
    )
    return message


class FakeDb:
    def __init__(self, status: str | None) -> None:
        self.status = status

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        if self.status is None:
            return None
        return {"telegram_id": telegram_id, "status": self.status}


async def test_throttling_allows_up_to_limit_then_blocks() -> None:
    middleware = ThrottlingMiddleware(SlidingWindowLimiter(limit=30, window_seconds=60))
    handler = AsyncMock(return_value="ok")
    event = MagicMock(spec=Message)
    event.answer = AsyncMock()
    data = {"event_from_user": USER}
    results = [await middleware(handler, event, data) for _ in range(31)]
    assert results[:30] == ["ok"] * 30
    assert results[30] is None
    assert handler.await_count == 30
    event.answer.assert_awaited_once()
    # Further throttled hits in the same window do not spam warnings.
    await middleware(handler, event, data)
    event.answer.assert_awaited_once()


async def test_throttling_is_per_user() -> None:
    middleware = ThrottlingMiddleware(SlidingWindowLimiter(limit=1, window_seconds=60))
    handler = AsyncMock(return_value="ok")
    event = MagicMock(spec=Message)
    event.answer = AsyncMock()
    other = User(id=2, is_bot=False, first_name="B")
    assert await middleware(handler, event, {"event_from_user": USER}) == "ok"
    assert await middleware(handler, event, {"event_from_user": other}) == "ok"


async def test_banned_middleware_blocks_everything_but_start_and_legal() -> None:
    middleware = BannedMiddleware(FakeDb("banned"))
    handler = AsyncMock(return_value="ok")
    for text, allowed in [
        ("/start", True),
        ("/start 123", True),
        ("/legal", True),
        ("/START@test_node_bot", True),
        ("/config", False),
        ("💳 Оплата", False),
    ]:
        handler.reset_mock()
        message = make_message(text)
        object.__setattr__(message, "answer", AsyncMock())
        result = await middleware(handler, message, {"event_from_user": USER})
        assert (result == "ok") is allowed, text


async def test_banned_middleware_blocks_callbacks() -> None:
    middleware = BannedMiddleware(FakeDb("banned"))
    handler = AsyncMock(return_value="ok")
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    assert await middleware(handler, callback, {"event_from_user": USER}) is None
    callback.answer.assert_awaited_once()


async def test_banned_middleware_passes_active_and_unknown_users() -> None:
    handler = AsyncMock(return_value="ok")
    data: dict[str, Any] = {"event_from_user": USER}
    assert (
        await BannedMiddleware(FakeDb("active"))(handler, make_message("/config"), data)
        == "ok"
    )
    assert data["db_user"]["status"] == "active"
    assert (
        await BannedMiddleware(FakeDb(None))(
            handler, make_message("/start"), {"event_from_user": USER}
        )
        == "ok"
    )


def test_is_allowed_for_banned_non_text() -> None:
    assert not is_allowed_for_banned(make_message(""))


def test_parse_start_arg() -> None:
    assert parse_start_arg(None, 1) == (None, None, None, False)
    assert parse_start_arg("555", 1) == (555, None, None, False)
    assert parse_start_arg("1", 1) == (None, None, None, False)
    assert parse_start_arg("pay", 1) == (None, None, None, True)
    # "pay_<months>" predates tiers: it means the basic tier.
    # Without a tier the handler picks the first tier on sale.
    assert parse_start_arg("pay_3", 1) == (None, None, 3, True)
    assert parse_start_arg("pay_5", 1) == (None, None, 5, True)
    assert parse_start_arg("pay_7", 1) == (None, None, None, True)
    assert parse_start_arg("pay_standard_2", 1) == (None, "standard", 2, True)
    assert parse_start_arg("pay_complete", 1) == (None, "complete", None, True)
    assert parse_start_arg("pay_prime_12", 1) == (None, "prime", 12, True)
    assert parse_start_arg("pay_gold_12", 1) == (None, None, 12, True)
    assert parse_start_arg("x" * 10, 1) == (None, None, None, False)
    assert parse_start_arg("9" * 25, 1) == (None, None, None, False)


def test_welcome_message_escapes_telegram_html() -> None:
    assert welcome_message("Алиса <3") == "👋 <b>Добро пожаловать, Алиса &lt;3!</b>"
    assert welcome_message("", returning=True) == "👋 <b>Рады снова видеть, друг!</b>"


def test_usdt_payment_questions_open_the_private_payment_flow() -> None:
    assert is_usdt_payment_request("Можно оплатить подписку в USDT?")
    assert is_usdt_payment_request("USDT?")
    assert not is_usdt_payment_request("Купил USDT на бирже")
    assert not is_usdt_payment_request("Как пользоваться ботом?")
