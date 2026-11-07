"""Read-only checks run inside the live bot container by `update_bot.sh --doctor`.

Prints one line per check: "OK ...", "WARN ..." or "FAIL ...". Never prints a
secret: errors are reported by type only, because an HTTP error text may
contain a URL with the bot token. Works with older images: missing settings
are read with getattr defaults.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

sys.path.insert(0, "/app")


def line(level: str, text: str) -> None:
    print(f"{level} {text}", flush=True)


def check_settings(s: Any) -> bool:
    """Settings the offer and the privacy policy rely on. True if REST is set."""
    line("OK", f"bot @{s.bot_username}, mode {getattr(s, 'bot_mode', '?')}")
    tiers = list(getattr(s, "enabled_tiers", []))
    line(
        "OK" if tiers == ["standard"] else "WARN",
        f"tariffs on sale: {', '.join(tiers) or 'none'}"
        + ("" if tiers == ["standard"] else ". Fix: --set-tiers standard"),
    )
    if getattr(s, "activity_logging_enabled", False):
        line(
            "FAIL",
            "ACTIVITY_LOGGING_ENABLED=true: a connection log is kept, but the offer "
            "and the privacy policy say it is not. Fix: "
            "--set ACTIVITY_LOGGING_ENABLED=false",
        )
    else:
        line("OK", "connection log is off, as the offer says")
    mode = getattr(s, "autoban_mode", "review")
    if mode == "review":
        line("OK", "AUTOBAN_MODE=review: an admin confirms every ban")
    else:
        line(
            "FAIL",
            f"AUTOBAN_MODE={mode}: the privacy policy says an admin reviews bans. "
            "Fix: --set AUTOBAN_MODE=review",
        )
    rest = s.sbp_provider == "yookassa" and bool(
        s.yookassa_shop_id and s.yookassa_secret_key
    )
    if rest:
        line("OK", "YooKassa API keys are set (SBP payments)")
    else:
        line("FAIL", "YooKassa API keys are missing: rubles by SBP cannot be taken")
    in_bot = getattr(s, "yookassa_in_bot", True)
    line(
        "OK" if in_bot else "WARN",
        f"ruble button in the bot: {'on' if in_bot else 'off'}",
    )
    line(
        "OK", f"Telegram Stars: {'on' if getattr(s, 'stars_enabled', False) else 'off'}"
    )
    if getattr(s, "crypto_pay_token", None):
        line(
            "WARN",
            "USDT (Crypto Pay) is on: it is not in the offer, and 259-FZ forbids "
            "residents to take crypto for services. Turn off: --disable-usdt",
        )
    else:
        line("OK", "USDT (Crypto Pay): off")
    return rest


async def check_yookassa(s: Any) -> None:
    import httpx

    egress = getattr(s, "yookassa_egress", "direct")
    proxy = getattr(s, "yookassa_proxy_url", None)
    try:
        async with httpx.AsyncClient(
            auth=(str(s.yookassa_shop_id), s.yookassa_secret_key.get_secret_value()),
            proxy=proxy,
            timeout=10,
            follow_redirects=False,
        ) as client:
            response = await client.get("https://api.yookassa.ru/v3/me")
    except Exception as exc:
        line("FAIL", f"YooKassa API via {egress}: {type(exc).__name__}")
        return
    if response.status_code != 200:
        line("FAIL", f"YooKassa API via {egress}: HTTP {response.status_code}")
        return
    line("OK", f"YooKassa API reachable via {egress}")
    data = response.json()
    if not isinstance(data, dict):
        line("WARN", "YooKassa account: unexpected answer")
        return
    status = data.get("status")
    line("OK" if status == "enabled" else "FAIL", f"YooKassa shop status: {status}")
    if data.get("test") is True:
        line("FAIL", "YooKassa shop is in TEST mode: real payments are not taken")
    else:
        line("OK", "YooKassa shop is live")
    methods = data.get("payment_methods")
    if isinstance(methods, list):
        if "sbp" in methods:
            line("OK", "SBP is enabled in the YooKassa shop")
        else:
            line("FAIL", "SBP is NOT enabled in the YooKassa shop: turn it on there")


async def check_marzban(s: Any) -> None:
    from services.marzban import MarzbanClient

    client = MarzbanClient(
        s.marzban_url,
        s.marzban_username,
        s.marzban_password.get_secret_value(),
        s.marzban_inbound_tag,
        verify_tls=s.marzban_verify_tls,
        timeout=10,
    )
    try:
        await client.get_core()
        nodes = await client.get_nodes()
        line("OK", f"Marzban panel answers, nodes: {len(nodes)}")
    except Exception as exc:
        line("FAIL", f"Marzban panel: {type(exc).__name__}")
    finally:
        await client.close()


async def check_telegram(s: Any) -> None:
    import httpx

    base = f"https://api.telegram.org/bot{s.bot_token.get_secret_value()}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            me = (await client.get(f"{base}/getMe")).json().get("result") or {}
            name = me.get("username")
            if name == s.bot_username:
                line("OK", f"Telegram answers as @{name}")
            else:
                line(
                    "FAIL", f"Telegram bot is @{name}, BOT_USERNAME is {s.bot_username}"
                )
            if getattr(s, "bot_mode", "polling") == "webhook":
                info = (await client.get(f"{base}/getWebhookInfo")).json()
                result = info.get("result") or {}
                error = result.get("last_error_message")
                pending = result.get("pending_update_count")
                if error:
                    line(
                        "WARN",
                        f"Telegram webhook: {pending} waiting, "
                        f"last error: {error[:80]}",
                    )
                else:
                    line("OK", f"Telegram webhook: {pending} waiting, no errors")
    except Exception as exc:
        line("FAIL", f"Telegram API: {type(exc).__name__}")


async def main() -> None:
    try:
        from config import get_settings

        settings = get_settings()
    except Exception as exc:
        line("FAIL", f"bot settings cannot be read: {type(exc).__name__}")
        return
    rest = check_settings(settings)
    if rest:
        await check_yookassa(settings)
    await check_marzban(settings)
    await check_telegram(settings)


if __name__ == "__main__":
    asyncio.run(main())
