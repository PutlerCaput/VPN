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


async def test_telegram_s_own_outage_is_retried_not_fatal() -> None:
    # 14 Nov 2026: Telegram answered "Bad Gateway" for this bot for a while; the
    # error escaped, the process crashed and restarted 30 times, and the site's
    # API in the same process went down each time.
    from aiogram.exceptions import TelegramRetryAfter, TelegramServerError

    calls = 0

    async def outage() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TelegramServerError(method=DeleteWebhook(), message="Bad Gateway")
        if calls == 2:
            raise TelegramRetryAfter(
                method=DeleteWebhook(), message="Too Many Requests", retry_after=1
            )
        if calls == 3:
            raise TimeoutError
        return True

    assert await main.call_with_retry(outage, asyncio.Event(), "delete_webhook")
    assert calls == 4
