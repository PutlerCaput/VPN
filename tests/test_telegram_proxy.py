"""Bot API calls leave through the configured Telegram proxy and nowhere else."""

from __future__ import annotations

import asyncio

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from pydantic import ValidationError

from config import Settings
from services.telegram_api import bot_session

FAKE_TOKEN = "123456:TEST"


@pytest.mark.parametrize(
    "url",
    [
        "https://203.0.113.10:3128",
        "http://login:password@203.0.113.10:3128",
        "http://api.telegram.org:3128",
        "http://203.0.113.10",
        "http://203.0.113.10:3128/path",
        "http://203.0.113.10:3128?x=1",
        "socks5://203.0.113.10:1080",
    ],
)
def test_rejects_anything_but_a_plain_http_proxy(url: str) -> None:
    with pytest.raises(ValidationError, match="telegram_proxy_url"):
        Settings(_env_file=None, telegram_proxy_url=url)


def test_accepts_a_plain_http_proxy_and_normalises_it() -> None:
    settings = Settings(_env_file=None, telegram_proxy_url="http://203.0.113.10:3128/")
    assert settings.telegram_proxy_url == "http://203.0.113.10:3128"


def test_blank_or_missing_value_means_direct_connection() -> None:
    assert Settings(_env_file=None, telegram_proxy_url="").telegram_proxy_url is None
    assert Settings(_env_file=None).telegram_proxy_url is None


def test_no_proxy_keeps_the_default_session() -> None:
    assert bot_session(None) is None
    session = bot_session("http://203.0.113.10:3128")
    assert isinstance(session, AiohttpSession)
    assert session.proxy == "http://203.0.113.10:3128"


async def test_bot_api_calls_leave_as_connect_to_telegram_through_the_proxy() -> None:
    seen: list[bytes] = []

    async def refuse(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(refuse, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    session = bot_session(f"http://127.0.0.1:{port}")
    assert session is not None
    bot = Bot(token=FAKE_TOKEN, session=session)
    try:
        with pytest.raises(TelegramNetworkError):
            await bot.get_me()
    finally:
        await bot.session.close()
        server.close()
        await server.wait_closed()
    assert seen
    assert seen[0].startswith(b"CONNECT api.telegram.org:443 ")
