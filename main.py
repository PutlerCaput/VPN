"""Entry point: bot (polling or webhook) + aiohttp server + scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiohttp import web
from pydantic import ValidationError
from pydantic_settings import SettingsError

from config import Settings, get_settings
from db.database import Database, DatabaseKeyError
from handlers import build_root_router
from logging_setup import setup_logging
from middlewares import BannedMiddleware, ThrottlingMiddleware
from services.awg import AwgAgent, AwgService
from services.billing import BillingService
from services.container import Services
from services.cryptopay import CryptoPayClient
from services.health import ServiceHealth
from services.marzban import MarzbanClient
from services.notifier import Notifier
from services.payment_reconciliation import PaymentReconciler
from services.provisioning import ProvisioningService
from services.push import PushService
from services.sbp import SbpService
from services.scheduler import AnomalyMonitor, Jobs, build_scheduler
from services.stars import StarsPayments
from services.telegram_api import bot_session
from services.telegram_rub import TelegramRubPayments
from services.yookassa import YooKassaClient
from webhook_server import TELEGRAM_WEBHOOK_PATH, create_webhook_app

logger = logging.getLogger("main")


def build_services(settings: Settings, db: Database, bot: Bot) -> Services:
    marzban = MarzbanClient(
        settings.marzban_url,
        settings.marzban_username,
        settings.marzban_password.get_secret_value(),
        settings.marzban_inbound_tag,
        verify_tls=settings.marzban_verify_tls,
        timeout=settings.marzban_timeout,
    )
    cryptopay = (
        CryptoPayClient(
            settings.crypto_pay_token.get_secret_value(), settings.crypto_pay_base_url
        )
        if settings.crypto_pay_token
        else None
    )
    sbp = SbpService(
        settings.sbp_provider,
        settings.sbp_qr_url,
        settings.sbp_payment_url,
        settings.sbp_provider_token.get_secret_value()
        if settings.sbp_provider_token
        else None,
    )
    push = PushService(
        db,
        settings.vapid_public,
        settings.vapid_private.get_secret_value() if settings.vapid_private else None,
        settings.vapid_contact,
    )
    notifier = Notifier(bot, push)
    provisioning = ProvisioningService(db, marzban, notifier, settings)
    yookassa = (
        YooKassaClient(
            settings.yookassa_shop_id,
            settings.yookassa_secret_key.get_secret_value(),
            return_url=settings.yookassa_return,
            receipt_mode=settings.yookassa_receipt_mode,
            vat_code=settings.yookassa_vat_code,
            tax_system_code=settings.yookassa_tax_system_code,
            proxy_url=settings.yookassa_proxy_url,
            payment_method="sbp",
        )
        if settings.sbp_provider == "yookassa"
        and settings.yookassa_shop_id
        and settings.yookassa_secret_key
        else None
    )
    return Services(
        settings=settings,
        db=db,
        bot=bot,
        marzban=marzban,
        cryptopay=cryptopay,
        sbp=sbp,
        push=push,
        notifier=notifier,
        provisioning=provisioning,
        billing=BillingService(
            db,
            sbp,
            cryptopay,
            yookassa,
            enabled_tiers=settings.enabled_tiers,
            stars_rub_per_star=(
                settings.stars_rub_per_star if settings.stars_enabled else None
            ),
        ),
        monitor=AnomalyMonitor(db, marzban, provisioning, settings),
        yookassa=yookassa,
        stars=(
            StarsPayments(db, provisioning, bot, settings.admin_telegram_id)
            if settings.stars_enabled
            else None
        ),
        health=ServiceHealth(
            marzban, notifier, settings.admin_telegram_id, settings.main_location_name
        ),
        telegram_rub=(
            TelegramRubPayments(
                db,
                provisioning,
                settings.admin_telegram_id,
                settings.telegram_payment_token.get_secret_value(),
            )
            if settings.telegram_payment_token is not None
            else None
        ),
        awg=(
            AwgService(
                db,
                AwgAgent(
                    settings.awg_agent_url,
                    settings.awg_agent_token.get_secret_value(),
                ),
                marzban,
                settings,
            )
            if settings.awg_enabled and settings.awg_agent_token is not None
            else None
        ),
    )


BOT_COMMANDS = (
    ("start", "Главное меню"),
    ("status", "Срок подписки и трафик"),
    ("config", "Моя конфигурация"),
    ("connect", "Подключить устройство"),
    ("servers", "Серверы и их состояние"),
    ("amnezia", "AmneziaWG: ключи для устройств"),
    ("help", "Помощь с подключением"),
    ("support", "Вопросы и поддержка"),
    ("autorenew", "Автопродление"),
    ("paysupport", "Вопрос по оплате"),
    ("terms", "Условия (оферта)"),
    ("privacy", "Персональные данные"),
)


def build_dispatcher(services: Services) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["services"] = services
    throttling = ThrottlingMiddleware()
    services.limiters.append(throttling.limiter)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(throttling)
        observer.outer_middleware(BannedMiddleware(services.db))
    dp.include_router(build_root_router())
    return dp


def load_settings() -> Settings:
    """Loads settings or exits with field names only (no values, no traceback)."""
    try:
        return get_settings()
    except ValidationError as exc:
        lines = []
        for error in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(part) for part in error["loc"]) or "settings"
            lines.append(f"  - {field}: {error['msg']}")
        details = "\n".join(lines)
        sys.stderr.write(f"Configuration error, refusing to start:\n{details}\n")
        raise SystemExit(2) from None
    except SettingsError as exc:
        # Raised when a source cannot be read, e.g. a secret file the bot user
        # (uid 1000) has no permission to open. The message names the field only.
        cause = type(exc.__cause__).__name__ if exc.__cause__ else "error"
        sys.stderr.write(
            f"Configuration error, refusing to start:\n  - {exc} ({cause})\n"
            "  Secret files must be readable by uid 1000: "
            "sudo chown 1000:1000 secrets/*.txt\n"
        )
        raise SystemExit(2) from None


STARTUP_RETRY_INITIAL_DELAY = 2.0
STARTUP_RETRY_MAX_DELAY = 60.0


async def call_with_retry(
    call: Callable[[], Awaitable[object]], stop: asyncio.Event, what: str
) -> bool:
    """Retry a startup Telegram call until it succeeds or shutdown is requested.

    Only the exception class is logged: aiogram/aiohttp errors may embed the
    request URL, which contains the bot token.
    """
    delay = STARTUP_RETRY_INITIAL_DELAY
    while not stop.is_set():
        try:
            await call()
            return True
        except TelegramUnauthorizedError:
            raise
        except (TelegramNetworkError, OSError) as exc:  # TimeoutError is an OSError
            logger.warning(
                "telegram_unreachable",
                extra={"call": what, "error": type(exc).__name__, "retry_in": delay},
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        delay = min(delay * 2, STARTUP_RETRY_MAX_DELAY)
    return False


async def run() -> None:
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_dir)
    for feature in settings.disabled_features():
        logger.warning("feature_disabled", extra={"feature": feature})
    db = Database(
        settings.database_path,
        settings.db_key.get_secret_value() if settings.db_key else None,
        activity_logging_enabled=settings.activity_logging_enabled,
    )
    try:
        await db.connect()
    except DatabaseKeyError as exc:
        sys.stderr.write(f"Database error, refusing to start: {exc}\n")
        raise SystemExit(2) from None
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        session=bot_session(settings.telegram_proxy_url),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    services = build_services(settings, db, bot)
    dp = build_dispatcher(services)

    jobs = Jobs(
        db,
        services.provisioning,
        services.notifier,
        services.monitor,
        settings,
        services.limiters,
        PaymentReconciler(
            db, services.provisioning, services.yookassa, services.cryptopay
        ),
        stars=services.stars,
        health=services.health,
        awg=services.awg,
    )
    scheduler = build_scheduler(jobs)

    app = create_webhook_app(bot, dp, services)
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, settings.http_host, settings.http_port)
    await site.start()
    scheduler.start()
    logger.info(
        "started", extra={"mode": settings.bot_mode, "port": settings.http_port}
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    polling: asyncio.Task[None] | None = None
    exit_code = 0

    def on_polling_done(task: asyncio.Task[None]) -> None:
        nonlocal exit_code
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            logger.error("polling_failed", extra={"error": type(exc).__name__})
            exit_code = 1
        stop.set()

    try:
        with contextlib.suppress(Exception):
            # Best effort: the menu is a convenience, the commands work without it.
            await asyncio.wait_for(
                bot.set_my_commands(
                    [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS]
                ),
                timeout=10,
            )
        if settings.bot_mode == "webhook":
            secret = settings.telegram_webhook_secret
            await call_with_retry(
                lambda: bot.set_webhook(
                    f"{settings.public_base_url}{TELEGRAM_WEBHOOK_PATH}",
                    secret_token=secret.get_secret_value() if secret else None,
                    allowed_updates=dp.resolve_used_update_types(),
                    drop_pending_updates=False,
                ),
                stop,
                "set_webhook",
            )
        elif await call_with_retry(
            lambda: bot.delete_webhook(drop_pending_updates=False),
            stop,
            "delete_webhook",
        ):
            polling = asyncio.create_task(
                dp.start_polling(
                    bot,
                    handle_signals=False,
                    allowed_updates=dp.resolve_used_update_types(),
                ),
            )
            polling.add_done_callback(on_polling_done)
        await stop.wait()
    except TelegramUnauthorizedError:
        logger.error("telegram_unauthorized")
        sys.stderr.write("Telegram rejected BOT_TOKEN, refusing to start.\n")
        exit_code = 3
    finally:
        logger.info("shutting_down")
        if polling is not None and not polling.done():
            await dp.stop_polling()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await polling
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await services.marzban.close()
        if services.cryptopay is not None:
            await services.cryptopay.close()
        if services.yookassa is not None:
            await services.yookassa.close()
        if services.awg is not None:
            await services.awg.agent.close()
        await bot.session.close()
        await db.close()
        logger.info("stopped")
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    asyncio.run(run())
