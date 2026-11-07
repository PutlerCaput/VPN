"""Bot API session: direct, or through an HTTP CONNECT proxy when one is set."""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError


class _ProxiedSession(AiohttpSession):
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], *args: Any, **kwargs: Any
    ) -> Any:
        try:
            return await super().make_request(bot, method, *args, **kwargs)
        except (ProxyError, ProxyConnectionError, ProxyTimeoutError) as exc:
            # A refused or failed proxy hop is a network problem: polling retries
            # it, as it does a dropped connection, instead of stopping the bot.
            raise TelegramNetworkError(method=method, message=f"proxy: {exc}") from exc


def bot_session(proxy_url: str | None) -> AiohttpSession | None:
    """None keeps aiogram's default session.

    A proxy URL sends every Bot API call through the proxy as a CONNECT to
    api.telegram.org:443. TLS stays end to end, so the proxy sees the host and
    the traffic volume, never the content or the bot token (it is in the path,
    inside TLS).
    """
    if not proxy_url:
        return None
    return _ProxiedSession(proxy=proxy_url)
