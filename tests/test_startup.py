"""Startup Telegram calls retry on network errors and fail fast on a bad token."""

from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import DeleteWebhook

import main


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "STARTUP_RETRY_INITIAL_DELAY", 0.001)
    monkeypatch.setattr(main, "STARTUP_RETRY_MAX_DELAY", 0.002)


async def test_retries_until_success() -> None:
    calls = 0

    async def flaky() -> bool:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TelegramNetworkError(method=DeleteWebhook(), message="dns")
        return True

    assert await main.call_with_retry(flaky, asyncio.Event(), "delete_webhook")
    assert calls == 3


async def test_stops_when_shutdown_requested() -> None:
    stop = asyncio.Event()
    stop.set()

    async def never() -> None:
        raise AssertionError("must not be called after stop")

    assert not await main.call_with_retry(never, stop, "delete_webhook")


async def test_unauthorized_is_not_retried() -> None:
    async def bad_token() -> None:
        raise TelegramUnauthorizedError(method=DeleteWebhook(), message="Unauthorized")

    with pytest.raises(TelegramUnauthorizedError):
        await main.call_with_retry(bad_token, asyncio.Event(), "delete_webhook")
