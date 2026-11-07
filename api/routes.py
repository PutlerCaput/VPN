"""REST API v1 for the PWA and the Telegram Mini App."""

from __future__ import annotations

import logging
import time
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiohttp import web
from pydantic import ValidationError

from api.auth import (
    AuthError,
    ReplayGuard,
    TelegramIdentity,
    ip_fingerprint,
    validate_init_data,
    validate_login_widget,
)
from api.middleware import (
    LIMITS_KEY,
    SERVICES_KEY,
    TOKENS_KEY,
    json_error,
    unauthorized,
)
from api.schemas import LogoutIn, PaymentCreateIn, PaymentInBotIn, RefreshIn
from config import (
    TIER_PRICES_RUB,
    TIER_PRICES_USDT,
    TIERS,
    savings_percent,
    stars_price,
)
from handlers.legal import offer_text, privacy_text
from handlers.payments import bot_methods, channel_text
from keyboards.main_menu import channel_kb
from services.billing import BillingError
from services.marzban import MarzbanError
from services.provisioning import ProvisioningError
from services.security import sanitize_username
from services.yookassa import is_valid_email

logger = logging.getLogger("api")
routes = web.RouteTableDef()
REPLAY_KEY = web.AppKey("replay_guard", ReplayGuard)


def _days_remaining(expires_at: int | None) -> int:
    if not expires_at:
        return 0
    return max(0, int((int(expires_at) - time.time()) // 86400))


def _auth_failed(request: web.Request, reason: str) -> web.Response:
    services = request.app[SERVICES_KEY]
    logger.warning(
        "tma_auth_failed",
        extra={
            "event": "tma_auth_failed",
            "reason": reason,
            "ip_hash": ip_fingerprint(
                request.get("client_ip", "unknown"),
                services.settings.jwt_secret.get_secret_value(),
            ),
        },
    )
    return unauthorized()


async def _upsert_user(request: web.Request, identity: TelegramIdentity) -> None:
    services = request.app[SERVICES_KEY]
    db = services.db
    username = sanitize_username(identity.username)
    existing = await db.get_user(identity.telegram_id)
    if existing is None:
        referrer: int | None = None
        param = identity.start_param or ""
        if (
            services.settings.referral_acquisition_enabled
            and param.isdigit()
            and len(param) <= 20
            and int(param) != identity.telegram_id
            and await db.get_user(int(param)) is not None
        ):
            referrer = int(param)
        await db.create_user(identity.telegram_id, username, referrer)
    await db.update_profile(identity.telegram_id, username, identity.first_name)


# ------------------------------------------------------------------- auth
@routes.post("/auth/telegram")
async def auth_telegram(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    settings = services.settings
    try:
        body: Any = await request.json()
    except ValueError:
        return _auth_failed(request, "parse")
    if not isinstance(body, dict):
        return _auth_failed(request, "parse")
    bot_token = settings.bot_token.get_secret_value()
    max_age = settings.telegram_auth_max_age_seconds
    try:
        if "initData" in body:
            init_data = body.get("initData")
            if not isinstance(init_data, str):
                raise AuthError("parse")
            identity, auth_hash = validate_init_data(
                init_data, bot_token, max_age=max_age
            )
        else:
            identity, auth_hash = validate_login_widget(
                body, bot_token, max_age=max_age
            )
    except AuthError as exc:
        return _auth_failed(request, exc.reason)
    if not request.app[REPLAY_KEY].check_and_add(auth_hash):
        return _auth_failed(request, "replay")
    await _upsert_user(request, identity)
    pair = await request.app[TOKENS_KEY].issue(identity.telegram_id)
    return web.json_response(pair.as_dict())


@routes.post("/auth/refresh")
async def auth_refresh(request: web.Request) -> web.Response:
    try:
        body = RefreshIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return _auth_failed(request, "parse")
    try:
        pair = await request.app[TOKENS_KEY].rotate(body.refresh_token)
    except AuthError as exc:
        return _auth_failed(request, exc.reason)
    return web.json_response(pair.as_dict())


@routes.post("/auth/logout")
async def auth_logout(request: web.Request) -> web.Response:
    tokens = request.app[TOKENS_KEY]
    if request.get("session_id"):
        await tokens.revoke(request["session_id"])
    if request.can_read_body:
        try:
            body = LogoutIn.model_validate(await request.json())
        except (ValueError, ValidationError):
            body = LogoutIn()
        if body.refresh_token:
            try:
                claims = tokens.decode(body.refresh_token, "refresh")
                if claims["sub"] == request["telegram_id"]:
                    await tokens.revoke(claims["jti"])
            except AuthError:
                pass
    return web.Response(status=204)


# ---------------------------------------------------------------- profile
@routes.get("/me")
async def me(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    user = request["user"]
    telegram_id = request["telegram_id"]
    return web.json_response(
        {
            "telegram_id": telegram_id,
            "username": user["username"],
            "first_name": user["first_name"],
            "status": user["status"],
            "expires_at": user["expires_at"],
            "node_username": user["node_username"],
            "tier": user["tier"],
            "devices": TIERS[user["tier"]].devices if user["tier"] in TIERS else None,
            "offer_accepted": await services.db.has_accepted_offer(telegram_id),
            "trial_used": bool(user["trial_used"]),
            "referral_count": await services.db.count_referrals(telegram_id),
            "referral_enabled": services.settings.referral_acquisition_enabled,
            "referral_link": (
                f"{services.settings.referral_link_base}{telegram_id}"
                if services.settings.referral_acquisition_enabled
                else ""
            ),
            "bot_username": services.settings.bot_username,
        },
    )


@routes.get("/subscription")
async def subscription(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    user = request["user"]
    traffic = (
        await services.provisioning.traffic_status(user)
        if user["status"] == "active"
        else None
    )
    return web.json_response(
        {
            "status": user["status"],
            "expires_at": user["expires_at"],
            "days_remaining": _days_remaining(user["expires_at"])
            if user["status"] == "active"
            else 0,
            "node_username": user["node_username"],
            # null when the panel is unreachable; limit 0 means unlimited.
            "traffic": None
            if traffic is None
            else {
                "used_bytes": traffic.used_bytes,
                "limit_bytes": traffic.limit_bytes,
                "resets_every_30_days": traffic.reset_strategy == "month",
                "exhausted": traffic.limited,
            },
        },
    )


@routes.get("/config")
async def config(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    limiter = request.app[LIMITS_KEY]["config_user"]
    telegram_id = request["telegram_id"]
    if not limiter.hit(str(telegram_id)):
        return json_error(429, "rate_limited", {"Retry-After": "60"})
    user = request["user"]
    if user["status"] != "active":
        return json_error(403, "inactive")
    try:
        link = await services.provisioning.fresh_config(telegram_id)
    except (MarzbanError, ProvisioningError):
        link = user["config_link"]
    if not link:
        return json_error(503, "node_unavailable")
    return web.json_response(
        {"config_link": link, "qr_payload": link, "expires_at": user["expires_at"]},
    )


@routes.get("/servers")
async def servers(request: web.Request) -> web.Response:
    """Locations and their state; names only, nothing about other users."""
    services = request.app[SERVICES_KEY]
    if services.health is None:
        return json_error(503, "unavailable")
    report = await services.health.current()
    return web.json_response(
        {
            "checked_at": report.checked_at,
            "panel_ok": report.panel_ok,
            "subscription": services.settings.config_delivery == "subscription",
            "locations": [
                {"name": item.name, "online": item.online} for item in report.locations
            ],
        }
    )


# --------------------------------------------------------------- payments
@routes.get("/tariffs")
async def tariffs(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    enabled = services.settings.enabled_tiers
    return web.json_response(
        {
            "tiers": [
                {
                    "key": key,
                    "title": tier.title,
                    "devices": tier.devices,
                    "traffic_gb": tier.traffic_gb,
                    "priority_support": tier.priority_support,
                    "features": list(tier.features),
                    "prices": [
                        {
                            "months": m,
                            "rub": TIER_PRICES_RUB[key][m],
                            "usdt": f"{TIER_PRICES_USDT[key][m]:.2f}",
                            # Stars are paid only inside the bot.
                            "stars": stars_price(
                                key, m, services.settings.stars_rub_per_star
                            )
                            if services.settings.stars_enabled
                            else None,
                            "save_percent": savings_percent(key, m),
                        }
                        for m in sorted(TIER_PRICES_RUB[key])
                    ],
                }
                for key, tier in TIERS.items()
                if key in enabled
            ],
        },
    )


@routes.post("/payments/create")
async def payments_create(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    try:
        body = PaymentCreateIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return json_error(400, "invalid_body")
    if body.email is not None:
        if not is_valid_email(body.email):
            return json_error(400, "invalid_email")
        await services.db.set_receipt_email(request["telegram_id"], body.email)
    try:
        payment = await services.billing.create(
            request["telegram_id"], body.months, body.method, body.tier
        )
    except BillingError as exc:
        status = {
            "offer_not_accepted": 409,
            "receipt_email_required": 409,
            "payment_processing": 409,
            "banned": 403,
            "too_many_pending": 429,
        }.get(exc.code, 502)
        if exc.code in ("invalid_months", "invalid_method", "invalid_tier"):
            status = 400
        if exc.code == "usdt_unavailable":
            status = 503
        return json_error(status, exc.code)
    return web.json_response(
        {"payment_id": payment.payment_id, **payment.data}, status=201
    )


@routes.post("/payments/in-bot")
async def payments_in_bot(request: web.Request) -> web.Response:
    """Mini App: the bot sends its payment menu to the user's chat.

    Payments stay in the bot. A t.me deep link no longer closes the Mini App
    (Bot API 7.0+), so the chat opened underneath it and the button looked
    dead. The Mini App closes itself once this call succeeds.
    """
    services = request.app[SERVICES_KEY]
    telegram_id = request["telegram_id"]
    try:
        body = PaymentInBotIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return json_error(400, "invalid_body")
    tier: str | None = body.tier
    months = body.months
    if months is not None and tier is None:
        tier = services.settings.default_tier
    if tier is not None and tier not in services.settings.enabled_tiers:
        return json_error(400, "invalid_tier")
    if tier is not None and months is not None and months not in TIER_PRICES_RUB[tier]:
        return json_error(400, "invalid_months")
    if not await services.db.has_accepted_offer(telegram_id):
        return json_error(409, "offer_not_accepted")
    methods = bot_methods(services)
    if not methods:
        return json_error(503, "payments_unavailable")
    limiter = request.app[LIMITS_KEY]["bot_message_user"]
    if not limiter.hit(str(telegram_id)):
        retry = str(max(1, int(limiter.retry_after(str(telegram_id))) + 1))
        return json_error(429, "rate_limited", {"Retry-After": retry})
    try:
        await services.bot.send_message(
            telegram_id,
            channel_text(methods),
            reply_markup=channel_kb(months, tier, methods),
        )
    except TelegramForbiddenError:
        # The user never started the bot or blocked it: the deep link helps.
        return json_error(409, "bot_unreachable")
    except TelegramAPIError:
        logger.warning("payment_menu_send_failed")
        return json_error(502, "telegram_unavailable")
    return web.json_response({"sent": True}, status=202)


@routes.get("/payments/history")
async def payments_history(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    rows = await services.db.payment_history(request["telegram_id"], 20)
    return web.json_response(
        [
            {
                "payment_id": row["payment_id"],
                # XTR has no minor unit; RUB/USDT are stored in kopecks/cents.
                "amount": row["amount"]
                if row["currency"] == "XTR"
                else row["amount"] / 100,
                "currency": row["currency"],
                "months": row["months"],
                "tier": row["tier"],
                "method": row["method"],
                "status": row["status"],
                "created_at": row["created_at"],
                "paid_at": row["paid_at"],
            }
            for row in rows
        ],
    )


# --------------------------------------------------------------- referral
@routes.get("/referral")
async def referral(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    telegram_id = request["telegram_id"]
    return web.json_response(
        {
            "enabled": services.settings.referral_acquisition_enabled,
            "link": (
                f"{services.settings.referral_link_base}{telegram_id}"
                if services.settings.referral_acquisition_enabled
                else ""
            ),
            "invited_count": await services.db.count_referrals(telegram_id),
            "bonus_days": await services.db.referral_bonus_days(telegram_id),
            "inviter_bonus_days": services.settings.referral_inviter_bonus_days,
            "invitee_bonus_days": services.settings.referral_invitee_bonus_days,
        },
    )


# ------------------------------------------------------------------ legal
@routes.get("/legal/offer")
async def legal_offer(request: web.Request) -> web.Response:
    response = web.json_response({"text": offer_text()})
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@routes.get("/legal/privacy")
async def legal_privacy(request: web.Request) -> web.Response:
    response = web.json_response({"text": privacy_text()})
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@routes.post("/legal/accept")
async def legal_accept(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    result = await services.provisioning.accept_offer_and_start_trial(
        request["telegram_id"]
    )
    if result.reason == "banned":
        return json_error(403, "banned")
    return web.json_response(
        {
            "accepted": True,
            "trial": result.reason,
            "expires_at": result.expires_at,
        },
    )
