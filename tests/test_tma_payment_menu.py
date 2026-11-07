"""Mini App pay buttons: the bot sends its payment menu to the user's chat."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from aiohttp.test_utils import TestClient, TestServer

from config import TIER_PRICES_RUB, TIERS
from handlers.payments import bot_methods
from tests.conftest import make_init_data
from webhook_server import create_webhook_app

USER = 770077
URL = "/api/v1/payments/in-bot"


async def _client(
    services: Any, *, accept: bool = True
) -> tuple[TestClient, dict[str, str]]:
    app = create_webhook_app(services.bot, None, services)
    client = TestClient(TestServer(app))
    await client.start_server()
    token = services.settings.bot_token.get_secret_value()
    response = await client.post(
        "/api/v1/auth/telegram", json={"initData": make_init_data(token, USER)}
    )
    assert response.status == 200
    headers = {"Authorization": f"Bearer {(await response.json())['access_token']}"}
    if accept:
        assert (
            await client.post("/api/v1/legal/accept", headers=headers)
        ).status == 200
    return client, headers


def _callbacks(call: Any) -> list[str]:
    markup = call.kwargs["reply_markup"]
    return [row[0].callback_data for row in markup.inline_keyboard]


async def test_menu_for_tier_and_period_is_sent_to_the_chat(services: Any) -> None:
    services.bot.send_message = AsyncMock()
    tier = services.settings.default_tier
    months = min(TIER_PRICES_RUB[tier])
    client, headers = await _client(services)
    try:
        response = await client.post(
            URL, json={"tier": tier, "months": months}, headers=headers
        )
        assert response.status == 202
    finally:
        await client.close()
    services.bot.send_message.assert_awaited_once()
    call = services.bot.send_message.await_args
    assert call.args[0] == USER
    assert "Оплата подписки" in call.args[1]
    methods = bot_methods(services)
    assert methods
    assert _callbacks(call) == [f"chan:{m}:{tier}:{months}" for m in methods]


async def test_empty_body_sends_the_general_menu(services: Any) -> None:
    services.bot.send_message = AsyncMock()
    client, headers = await _client(services)
    try:
        assert (await client.post(URL, json={}, headers=headers)).status == 202
    finally:
        await client.close()
    call = services.bot.send_message.await_args
    assert _callbacks(call) == [f"chan:{m}" for m in bot_methods(services)]


async def test_offer_must_be_accepted_first(services: Any) -> None:
    services.bot.send_message = AsyncMock()
    client, headers = await _client(services, accept=False)
    try:
        response = await client.post(URL, json={}, headers=headers)
        assert response.status == 409
        assert (await response.json())["error"] == "offer_not_accepted"
    finally:
        await client.close()
    services.bot.send_message.assert_not_awaited()


async def test_unknown_period_and_disabled_tier_are_rejected(services: Any) -> None:
    services.bot.send_message = AsyncMock()
    tier = services.settings.default_tier
    missing = next(m for m in range(1, 13) if m not in TIER_PRICES_RUB[tier])
    disabled = next(
        (key for key in TIERS if key not in services.settings.enabled_tiers), None
    )
    client, headers = await _client(services)
    try:
        bad_period = await client.post(
            URL, json={"tier": tier, "months": missing}, headers=headers
        )
        assert bad_period.status == 400
        assert (await bad_period.json())["error"] == "invalid_months"
        if disabled is not None:
            bad_tier = await client.post(URL, json={"tier": disabled}, headers=headers)
            assert bad_tier.status == 400
            assert (await bad_tier.json())["error"] == "invalid_tier"
        junk = await client.post(URL, json={"tier": "gold"}, headers=headers)
        assert junk.status == 400
    finally:
        await client.close()
    services.bot.send_message.assert_not_awaited()


async def test_repeated_taps_are_rate_limited(services: Any) -> None:
    services.bot.send_message = AsyncMock()
    client, headers = await _client(services)
    try:
        statuses = [
            (await client.post(URL, json={}, headers=headers)).status for _ in range(6)
        ]
    finally:
        await client.close()
    assert statuses == [202] * 5 + [429]
    assert services.bot.send_message.await_count == 5


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            TelegramForbiddenError(
                method=SendMessage(chat_id=USER, text="x"), message="blocked"
            ),
            409,
            "bot_unreachable",
        ),
        (
            TelegramNetworkError(
                method=SendMessage(chat_id=USER, text="x"), message="timeout"
            ),
            502,
            "telegram_unavailable",
        ),
    ],
)
async def test_telegram_errors_tell_the_app_to_use_the_deep_link(
    services: Any, error: Exception, status: int, code: str
) -> None:
    services.bot.send_message = AsyncMock(side_effect=error)
    client, headers = await _client(services)
    try:
        response = await client.post(URL, json={}, headers=headers)
        assert response.status == status
        assert (await response.json())["error"] == code
    finally:
        await client.close()


async def test_me_reports_the_bot_username_for_the_fallback(services: Any) -> None:
    client, headers = await _client(services, accept=False)
    try:
        me = await (await client.get("/api/v1/me", headers=headers)).json()
    finally:
        await client.close()
    assert me["bot_username"] == services.settings.bot_username
