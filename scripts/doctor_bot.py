"""Read-only checks run inside the live bot container by `update_bot.sh --doctor`.

Prints one line per check: "OK ...", "WARN ..." or "FAIL ...". Never prints a
secret: errors are reported by type only, because an HTTP error text may
contain a URL with the bot token. Works with older images: missing settings
are read with getattr defaults.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")
# Five years, the term in the policy's section 7.
RETENTION_DAYS = 5 * 365


def privacy_path() -> Path:
    """The policy the running bot publishes: /app/legal in its image.

    update_bot.sh pipes this file into the container, so there is no __file__
    there to start from. Run from a checkout, the repository's copy is read;
    DOCTOR_PRIVACY_PATH points the tests at a file of their own.
    """
    override = os.environ.get("DOCTOR_PRIVACY_PATH")
    if override:
        return Path(override)
    image = Path("/app/legal/privacy.md")
    here = globals().get("__file__")
    if image.is_file() or not here:
        return image
    return Path(here).resolve().parent.parent / "legal" / "privacy.md"


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
        if getattr(s, "yookassa_confirmation", "redirect") == "qr":
            line("OK", "SBP by QR: the bot shows the QR code and a bank-app button")
        else:
            line(
                "OK",
                "SBP through the YooMoney checkout page. If the page stays blank "
                "for payers: --set YOOKASSA_CONFIRMATION=qr",
            )
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
            "USDT (Crypto Pay) is on. It is not in the offer. Whether residents may "
            "take crypto for services under 259-FZ needs a lawyer. Turn off: "
            "--disable-usdt",
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
        try:
            code = str(response.json().get("code", ""))
        except (ValueError, AttributeError):
            code = ""
        status_text = f"HTTP {response.status_code}" + (f" {code}" if code else "")
        hint = ""
        if response.status_code in (401, 403):
            hint = (
                ". Fix: the shop ID (.env, YOOKASSA_SHOP_ID) and the key "
                "(secrets/yookassa_secret_key.txt, a Docker secret that "
                "overrides .env) must belong to the same shop. "
                "See docs/FINISH_RUNBOOK.md, «Ключи ЮKassa»"
            )
        line("FAIL", f"YooKassa API via {egress}: {status_text}{hint}")
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
        async with httpx.AsyncClient(
            timeout=10,
            proxy=getattr(s, "telegram_proxy_url", None),
        ) as client:
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


def policy_keeps_connection_records(text: str | None = None) -> bool | None:
    """What the published policy says about connection records; None if unreadable."""
    if text is None:
        try:
            text = privacy_path().read_text(encoding="utf-8")
        except OSError:
            return None
    listed_as_not_processed = "журнал подключений;" in text
    return "Учёт подключений" in text and not listed_as_not_processed


async def check_database(s: Any) -> bool | None:
    """Where the data is and whether it answers.

    Returns the connection-records switch that is actually in force: the one
    next to the data. None when it could not be read.
    """
    url = getattr(s, "database_url", None)
    token = getattr(s, "db_gateway_token", None)
    if not url or token is None:
        key = "encrypted" if getattr(s, "db_key", None) else "NOT encrypted"
        line("OK" if key == "encrypted" else "WARN", f"database: local file, {key}")
        return bool(getattr(s, "activity_logging_enabled", False))
    try:
        from db.remote import (
            MAX_CLOCK_SKEW_SECONDS,
            DatabaseUnavailableError,
            RemoteDatabase,
            RemoteDatabaseError,
        )
    except ImportError:
        line("FAIL", "DATABASE_URL is set, but this release cannot use a gateway")
        return None
    client = RemoteDatabase(url, token.get_secret_value(), retries=0)
    try:
        await client.connect()
    except DatabaseUnavailableError:
        line(
            "FAIL",
            "database gateway does not answer: check the tunnel between the "
            "servers (db_cutover.sh status)",
        )
        return None
    except RemoteDatabaseError as exc:
        line("FAIL", f"database gateway refused the bot: {exc}")
        return None
    else:
        key = "encrypted" if client.encrypted else "NOT encrypted"
        line(
            "OK" if client.encrypted else "FAIL",
            f"database: on the other server through the gateway, {key}",
        )
        skew = abs(client.clock_skew_seconds)
        if skew > MAX_CLOCK_SKEW_SECONDS:
            line("WARN", f"the two servers' clocks differ by {skew} s: fix their time")
        if getattr(client, "tokens_accepted", 1) > 1:
            line(
                "WARN",
                "the gateway still accepts two tokens: a token rotation was started "
                "and not finished (RU: db_gateway_install.sh --finish-token-rotation)",
            )
        return bool(client.activity_logging_enabled)
    finally:
        await client.close()


def check_connection_records(s: Any, switch: bool | None) -> None:
    """The switch and the published policy must say the same thing."""
    remote = bool(getattr(s, "database_url", None))
    turn_on = (
        "db_cutover.sh connection-log on"
        if remote
        else "--set ACTIVITY_LOGGING_ENABLED=true"
    )
    turn_off = (
        "db_cutover.sh connection-log off"
        if remote
        else "--set ACTIVITY_LOGGING_ENABLED=false"
    )
    policy = policy_keeps_connection_records()
    if switch is None or policy is None:
        line("WARN", "connection records: the switch or the policy could not be read")
    elif switch and policy:
        line("OK", "connection records are kept, as the policy says")
    elif switch:
        line(
            "FAIL",
            "connection records are kept, but the published policy says they are "
            f"not. Publish the current policy (update the bot) or turn off: {turn_off}",
        )
    elif policy:
        line(
            "WARN",
            "the policy says connection records are kept, but the switch is off: "
            f"nothing is recorded. Turn on: {turn_on}",
        )
    else:
        line("OK", "connection records are off, as the policy says")
    days = int(getattr(s, "conn_log_retention_days", RETENTION_DAYS))
    if days < RETENTION_DAYS:
        line(
            "WARN",
            f"CONN_LOG_RETENTION_DAYS={days}: records would be deleted before the "
            "5 years the policy names. Fix: --set CONN_LOG_RETENTION_DAYS=1826",
        )


async def main() -> None:
    try:
        from config import get_settings

        settings = get_settings()
    except Exception as exc:
        line("FAIL", f"bot settings cannot be read: {type(exc).__name__}")
        return
    rest = check_settings(settings)
    check_connection_records(settings, await check_database(settings))
    if rest:
        await check_yookassa(settings)
    await check_marzban(settings)
    await check_telegram(settings)


if __name__ == "__main__":
    if sys.argv[1:] == ["--policy-keeps-connection-records"]:
        # For db_cutover.sh: may the connection records switch be turned on?
        raise SystemExit(0 if policy_keeps_connection_records() else 1)
    asyncio.run(main())
