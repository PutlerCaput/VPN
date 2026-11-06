"""aiohttp application: payment webhooks, abuse reports, health, metrics, API."""

from __future__ import annotations

import hmac
import json
import logging
from decimal import Decimal, DecimalException
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from api import register_api_routes
from api.schemas import AbuseReportIn
from services.container import Services
from services.cryptopay import parse_paid_invoice, verify_webhook_signature
from services.metrics import REGISTRY, WEBHOOK_REJECTED
from services.payment_reconciliation import reconcile_yookassa
from services.sbp import SBP_SIGNATURE_HEADER
from services.yookassa import YooKassaError, notification_payment_id

logger = logging.getLogger("webhooks")

SERVICES = web.AppKey("services", Services)
MAX_WEBHOOK_BODY = 64 * 1024
TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"


async def _read_limited(request: web.Request) -> bytes | None:
    if request.content_length is not None and request.content_length > MAX_WEBHOOK_BODY:
        return None
    body = await request.content.read(MAX_WEBHOOK_BODY + 1)
    return None if len(body) > MAX_WEBHOOK_BODY else body


def _ok() -> web.Response:
    return web.json_response({"ok": True})


async def healthz(request: web.Request) -> web.Response:
    services = request.app[SERVICES]
    try:
        db_ok = await services.db.ping()
    except Exception:
        db_ok = False
    return web.json_response(
        {"status": "ok" if db_ok else "degraded", "db": db_ok},
        status=200 if db_ok else 503,
    )


async def metrics(request: web.Request) -> web.Response:
    token = request.app[SERVICES].settings.metrics_token
    if token is not None:
        expected = f"Bearer {token.get_secret_value()}"
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return web.Response(status=401)
    return web.Response(
        body=generate_latest(REGISTRY), headers={"Content-Type": CONTENT_TYPE_LATEST}
    )


async def cryptopay_webhook(request: web.Request) -> web.Response:
    services = request.app[SERVICES]
    if services.cryptopay is None:
        return web.Response(status=404)
    raw = await _read_limited(request)
    signature = request.headers.get("crypto-pay-api-signature")
    # Signature is verified on the raw bytes BEFORE any JSON parsing.
    if raw is None or not verify_webhook_signature(
        services.cryptopay.api_token, raw, signature
    ):
        WEBHOOK_REJECTED.labels(source="cryptopay").inc()
        logger.warning("cryptopay_bad_signature")
        return web.Response(status=401)
    try:
        update: Any = json.loads(raw)
    except ValueError:
        WEBHOOK_REJECTED.labels(source="cryptopay").inc()
        return web.Response(status=400)
    if not isinstance(update, dict):
        return web.Response(status=400)
    invoice = parse_paid_invoice(update)
    if invoice is None:
        return _ok()
    provider_id = f"cryptopay:{invoice.get('invoice_id')}"
    payment = await services.db.get_payment_by_provider(provider_id)
    if payment is None:
        logger.warning("cryptopay_unknown_invoice", extra={"provider_id": provider_id})
        return _ok()
    paid_amount = str(invoice.get("amount", ""))
    try:
        amount = Decimal(paid_amount) * 100
        amount_ok = amount.is_finite() and amount >= int(payment["amount"])
    except DecimalException:
        amount_ok = False
    if invoice.get("asset") != "USDT" or not amount_ok:
        WEBHOOK_REJECTED.labels(source="cryptopay").inc()
        logger.error(
            "cryptopay_amount_mismatch",
            extra={"payment_id": payment["payment_id"], "paid": paid_amount},
        )
        return _ok()
    await services.provisioning.process_paid_payment(payment["payment_id"])
    return _ok()


async def sbp_webhook(request: web.Request) -> web.Response:
    services = request.app[SERVICES]
    if services.sbp.is_manual:
        return web.Response(status=404)
    raw = await _read_limited(request)
    if raw is None or not services.sbp.verify_signature(
        raw, request.headers.get(SBP_SIGNATURE_HEADER)
    ):
        WEBHOOK_REJECTED.labels(source="sbp").inc()
        return web.Response(status=401)
    notification = services.sbp.parse_notification(raw)
    if notification is None:
        WEBHOOK_REJECTED.labels(source="sbp").inc()
        return web.Response(status=400)
    payment = await services.db.get_payment(notification.payment_id)
    if payment is None or payment["method"] != "sbp":
        return web.Response(status=404)
    if notification.amount_rub * 100 < int(payment["amount"]):
        WEBHOOK_REJECTED.labels(source="sbp").inc()
        logger.error(
            "sbp_amount_mismatch", extra={"payment_id": notification.payment_id}
        )
        return web.Response(status=422)
    if payment["provider_id"] is None:
        await services.db.set_payment_provider_id(
            notification.payment_id, f"sbp:{notification.provider_id}"
        )
    await services.provisioning.process_paid_payment(notification.payment_id)
    return _ok()


async def yookassa_webhook(request: web.Request) -> web.Response:
    """YooKassa notifications are unsigned: the body only names a payment, which
    is then re-read from the API with the shop credentials. Non-2xx makes
    YooKassa retry, so a 5xx is returned only when the API check itself fails."""
    services = request.app[SERVICES]
    if services.yookassa is None:
        return web.Response(status=404)
    raw = await _read_limited(request)
    try:
        update: Any = json.loads(raw or b"")
    except ValueError:
        WEBHOOK_REJECTED.labels(source="yookassa").inc()
        return web.Response(status=400)
    yookassa_id = notification_payment_id(update)
    if yookassa_id is None:
        return _ok()
    payment = await services.db.get_payment_by_provider(f"yookassa:{yookassa_id}")
    if payment is None:
        WEBHOOK_REJECTED.labels(source="yookassa").inc()
        logger.warning("yookassa_unknown_payment")
        # Creation may already have succeeded at YooKassa while our durable
        # creation request still awaits its response/provider-ID commit. Retry
        # rather than acknowledge a callback that cannot yet be associated.
        # Never query the provider for attacker-supplied unknown invoice IDs.
        return web.Response(status=503)
    try:
        result = await reconcile_yookassa(
            services.db, services.yookassa, services.provisioning, payment
        )
    except YooKassaError as exc:
        logger.error("yookassa_verify_failed", extra={"error": str(exc)})
        return web.Response(status=503)
    # The notification is only a hint. If the API has not exposed the final
    # captured state yet, return non-2xx so YooKassa retries it later.
    if result == "pending":
        return web.Response(status=503)
    if result == "mismatch":
        WEBHOOK_REJECTED.labels(source="yookassa").inc()
        logger.error(
            "yookassa_verification_mismatch",
            extra={"payment_id": payment["payment_id"]},
        )
        return _ok()
    return _ok()


async def abuse_report(request: web.Request) -> web.Response:
    """IDS hook on nodes: POST {"node_username", "kinds": [...]} with X-Abuse-Token."""
    services = request.app[SERVICES]
    token = services.settings.abuse_report_token
    if token is None:
        return web.Response(status=404)
    if not hmac.compare_digest(
        request.headers.get("X-Abuse-Token", ""), token.get_secret_value()
    ):
        return web.Response(status=401)
    raw = await _read_limited(request)
    try:
        report = AbuseReportIn.model_validate_json(raw or b"")
    except ValueError:
        return web.Response(status=400)
    banned = await services.monitor.handle_report(
        report.node_username,
        {k.upper() for k in report.kinds},
        {"source": report.source, "detail": report.detail},
    )
    return web.json_response({"banned": banned})


def create_webhook_app(bot: Bot, dp: Dispatcher, services: Services) -> web.Application:
    app = web.Application(client_max_size=MAX_WEBHOOK_BODY)
    app[SERVICES] = services
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/metrics", metrics)
    app.router.add_post("/cryptopay/webhook", cryptopay_webhook)
    app.router.add_post("/sbp/webhook", sbp_webhook)
    app.router.add_post("/yookassa/webhook", yookassa_webhook)
    app.router.add_post("/internal/abuse", abuse_report)
    settings = services.settings
    if settings.bot_mode == "webhook":
        secret = settings.telegram_webhook_secret
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=secret.get_secret_value() if secret else None,
        ).register(app, path=TELEGRAM_WEBHOOK_PATH)
    register_api_routes(app, services)
    return app
