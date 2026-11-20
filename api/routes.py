"""REST API v1 for the PWA and the Telegram Mini App."""

from __future__ import annotations

import logging
import time
import uuid
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
    CLIENT_IP_KEY,
    LIMITS_KEY,
    SERVICES_KEY,
    SESSION_ID_KEY,
    TELEGRAM_ID_KEY,
    TOKENS_KEY,
    USER_KEY,
    json_error,
    unauthorized,
)
from api.schemas import (
    EmailRequestIn,
    EmailVerifyIn,
    LogoutIn,
    PaymentCreateIn,
    PaymentInBotIn,
    RefreshIn,
)
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
from services.email_login import is_site_account, mask_email, normalize_email
from services.hiddify import HiddifyError
from services.mailer import MailError
from services.provisioning import ProvisioningError
from services.security import sanitize_username
from services.sharding import (
    FanoutGuard,
    derive_key,
    filter_subscription,
    plan_from_settings,
    resolve_segment,
)
from services.yookassa import is_valid_email

logger = logging.getLogger("api")
routes = web.RouteTableDef()
REPLAY_KEY = web.AppKey("replay_guard", ReplayGuard)
SUB_FANOUT_KEY = web.AppKey("sub_fanout", FanoutGuard)
# What the panel is asked for when the app's own format cannot be filtered
# (Clash YAML and the like): a plain link list every app also understands.
LINKS_USER_AGENT = "v2rayNG/1.9.0"
# Headers of the panel's answer that apps read (traffic, title, refresh period).
# Not profile-web-page-url: Hiddify points it at the user's own panel page
# (panel host, client path, UUID), where the unfiltered subscription and the
# whole entry pool can be read, which would undo the sharding.
SUB_PASS_HEADERS = (
    "subscription-userinfo",
    "profile-update-interval",
    "profile-title",
    "support-url",
    "content-disposition",
)


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
                request.get(CLIENT_IP_KEY) or "unknown",
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


# ----------------------------------------------------------- auth by email
def _email_login(request: web.Request) -> Any:
    login = request.app[SERVICES_KEY].email_login
    return login if login is not None and login.enabled else None


def _code_allowed(request: web.Request, email: str) -> bool:
    limits = request.app[LIMITS_KEY]
    ip = request.get(CLIENT_IP_KEY) or "unknown"
    by_ip = limits["email_code_ip"].hit(ip)
    by_address = limits["email_code_address"].hit(email)
    return bool(by_ip and by_address)


@routes.post("/auth/email/request")
async def auth_email_request(request: web.Request) -> web.Response:
    """Mails a login code. The answer never tells whether the address is known."""
    login = _email_login(request)
    if login is None:
        return json_error(503, "email_login_unavailable")
    try:
        body = EmailRequestIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return json_error(400, "invalid_email")
    email = normalize_email(body.email)
    if email is None:
        return json_error(400, "invalid_email")
    if not _code_allowed(request, email):
        return json_error(429, "rate_limited", {"Retry-After": "600"})
    try:
        await login.send_code(email, "login")
    except MailError:
        return json_error(502, "mail_unavailable")
    # Also when a code went out less than a minute ago: the earlier one is valid.
    return web.json_response({"sent": True}, status=202)


@routes.post("/auth/email/verify")
async def auth_email_verify(request: web.Request) -> web.Response:
    login = _email_login(request)
    if login is None:
        return json_error(503, "email_login_unavailable")
    try:
        body = EmailVerifyIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return _auth_failed(request, "parse")
    email = normalize_email(body.email)
    if email is None:
        return _auth_failed(request, "parse")
    outcome = await login.check(email, body.code, "login")
    if outcome["result"] != "ok":
        return _auth_failed(request, f"email_code_{outcome['result']}")
    services = request.app[SERVICES_KEY]
    telegram_id = await services.db.account_for_email(email)
    pair = await request.app[TOKENS_KEY].issue(telegram_id)
    return web.json_response(pair.as_dict())


@routes.post("/me/email/request")
async def me_email_request(request: web.Request) -> web.Response:
    """A signed-in user ties an address to the account, to sign in by it later."""
    login = _email_login(request)
    if login is None:
        return json_error(503, "email_login_unavailable")
    telegram_id = request[TELEGRAM_ID_KEY]
    services = request.app[SERVICES_KEY]
    try:
        body = EmailRequestIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return json_error(400, "invalid_email")
    email = normalize_email(body.email)
    if email is None:
        return json_error(400, "invalid_email")
    if await services.db.email_of(telegram_id) is not None:
        return json_error(409, "email_already_linked")
    if not _code_allowed(request, email):
        return json_error(429, "rate_limited", {"Retry-After": "600"})
    # An address that belongs to another account gets no code, and the answer is
    # the same: it must not tell who is registered.
    if not await services.db.email_taken(email):
        try:
            await login.send_code(email, "link", telegram_id)
        except MailError:
            return json_error(502, "mail_unavailable")
    return web.json_response({"sent": True}, status=202)


@routes.post("/me/email/verify")
async def me_email_verify(request: web.Request) -> web.Response:
    login = _email_login(request)
    if login is None:
        return json_error(503, "email_login_unavailable")
    telegram_id = request[TELEGRAM_ID_KEY]
    try:
        body = EmailVerifyIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return json_error(400, "invalid_code")
    email = normalize_email(body.email)
    if email is None:
        return json_error(400, "invalid_code")
    outcome = await login.check(email, body.code, "link")
    if outcome["result"] != "ok" or outcome["telegram_id"] != telegram_id:
        return json_error(400, "invalid_code")
    if not await request.app[SERVICES_KEY].db.link_email(email, telegram_id):
        return json_error(409, "email_already_linked")
    return web.json_response({"email": mask_email(email)})


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
    session_id = request.get(SESSION_ID_KEY)
    if session_id:
        await tokens.revoke(session_id)
    if request.can_read_body:
        try:
            body = LogoutIn.model_validate(await request.json())
        except (ValueError, ValidationError):
            body = LogoutIn()
        if body.refresh_token:
            try:
                claims = tokens.decode(body.refresh_token, "refresh")
                if claims["sub"] == request[TELEGRAM_ID_KEY]:
                    await tokens.revoke(claims["jti"])
            except AuthError:
                pass
    return web.Response(status=204)


# ---------------------------------------------------------------- profile
@routes.get("/me")
async def me(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    user = request[USER_KEY]
    telegram_id = request[TELEGRAM_ID_KEY]
    email = await services.db.email_of(telegram_id)
    return web.json_response(
        {
            "telegram_id": telegram_id,
            # "site": made by email on the site, nothing of Telegram behind it.
            "account": "site" if is_site_account(telegram_id) else "telegram",
            "email": mask_email(email) if email else None,
            "email_login": _email_login(request) is not None,
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
                and not is_site_account(telegram_id)
                else ""
            ),
            "bot_username": services.settings.bot_username,
        },
    )


@routes.get("/subscription")
async def subscription(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    user = request[USER_KEY]
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
    telegram_id = request[TELEGRAM_ID_KEY]
    if not limiter.hit(str(telegram_id)):
        return json_error(429, "rate_limited", {"Retry-After": "60"})
    user = request[USER_KEY]
    if user["status"] != "active":
        return json_error(403, "inactive")
    try:
        link = await services.provisioning.fresh_config(telegram_id)
    except (HiddifyError, ProvisioningError):
        link = user["config_link"]
    if not link:
        return json_error(503, "node_unavailable")
    return web.json_response(
        {
            "config_link": link,
            "qr_payload": link,
            "expires_at": user["expires_at"],
            # v2rayNG gets "Russian sites directly" with the subscription.
            "ru_direct_auto": services.settings.ru_direct_subscription,
        },
    )


def _sub_not_found() -> web.Response:
    return web.Response(status=404, text="not found")


def _node_uuid(value: str) -> str | None:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value.lower() else None


@routes.get("/sub/{node}")
@routes.get("/sub/{node}/")
async def sharded_subscription(request: web.Request) -> web.Response:
    """The user's Hiddify subscription with only their entry segment in it.

    Anti-parsing: the address is throttled (middleware), wrong UUIDs lock the
    address out for a while, each subscription has its own rate and a cap on
    distinct sources per day. Nothing about the request is stored: no IP, no
    UUID in the logs.
    """
    services = request.app[SERVICES_KEY]
    settings = services.settings
    if not settings.entry_sharding_enabled:
        return _sub_not_found()
    limits = request.app[LIMITS_KEY]
    ip = request[CLIENT_IP_KEY]
    misses = limits["sub_miss_ip"]
    if misses.retry_after(ip) > 0:
        return json_error(429, "rate_limited", {"Retry-After": "3600"})
    node = _node_uuid(request.match_info["node"])
    user = await services.db.get_user_by_node(node) if node else None
    if node is None or user is None:
        misses.hit(ip)
        return _sub_not_found()
    if user["status"] != "active":
        # A real subscription whose rental ended: same answer as an unknown
        # one, but not a guess. Its app keeps refreshing, and counting that
        # would lock out everyone behind the same carrier NAT address.
        return _sub_not_found()
    if not limits["sub_user"].hit(node):
        retry = str(max(1, int(limits["sub_user"].retry_after(node)) + 1))
        return json_error(429, "rate_limited", {"Retry-After": retry})
    if not request.app[SUB_FANOUT_KEY].allow(node, ip):
        logger.warning("sub_fanout_blocked")
        return json_error(429, "rate_limited", {"Retry-After": "3600"})

    plan = plan_from_settings(settings)
    segment = await resolve_segment(
        services.db,
        plan,
        int(user["telegram_id"]),
        derive_key(settings.jwt_secret.get_secret_value()),
    )
    agent = request.headers.get("User-Agent", "")
    try:
        status, body, headers = await services.hiddify.fetch_subscription(node, agent)
        filtered = (
            filter_subscription(
                body, plan, segment, fingerprint=settings.subscription_fingerprint
            )
            if status == 200
            else None
        )
        if status == 200 and filtered is None:
            status, body, headers = await services.hiddify.fetch_subscription(
                node, LINKS_USER_AGENT
            )
            filtered = (
                filter_subscription(
                    body, plan, segment, fingerprint=settings.subscription_fingerprint
                )
                if status == 200
                else None
            )
    except HiddifyError:
        logger.warning("sub_upstream_error")
        return json_error(502, "upstream_unavailable")
    if status in (403, 404):
        return _sub_not_found()
    if filtered is None:
        logger.warning("sub_unreadable", extra={"upstream_status": status})
        return json_error(502, "upstream_unavailable")
    if filtered.kept == 0:
        # An empty profile would wipe the app's working one: an error keeps it.
        logger.warning("sub_segment_empty", extra={"segment": segment})
        return json_error(503, "no_entries", {"Retry-After": "600"})
    lowered = {k.lower(): v for k, v in headers.items()}
    passed = {k: lowered[k] for k in SUB_PASS_HEADERS if k in lowered}
    logger.info(
        "sub_served",
        extra={"segment": segment, "kind": filtered.kind, "removed": filtered.removed},
    )
    return web.Response(
        text=filtered.body,
        content_type="application/json"
        if filtered.kind in ("singbox", "xray")
        else "text/plain",
        charset="utf-8",
        headers=passed,
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
            "status_source": "hiddify_panel",
            "vpn_verified": False,
            "failover_mode": "client_dependent",
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
        await services.db.set_receipt_email(request[TELEGRAM_ID_KEY], body.email)
    try:
        payment = await services.billing.create(
            request[TELEGRAM_ID_KEY], body.months, body.method, body.tier
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
    telegram_id = request[TELEGRAM_ID_KEY]
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
    if is_site_account(telegram_id):
        # No Telegram chat behind a site account: it pays on the site.
        return json_error(409, "bot_unreachable")
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
    rows = await services.db.payment_history(request[TELEGRAM_ID_KEY], 20)
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
    telegram_id = request[TELEGRAM_ID_KEY]
    return web.json_response(
        {
            "enabled": services.settings.referral_acquisition_enabled,
            "link": (
                f"{services.settings.referral_link_base}{telegram_id}"
                if services.settings.referral_acquisition_enabled
                and not is_site_account(telegram_id)
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
        request[TELEGRAM_ID_KEY]
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
