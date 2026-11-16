"""API middlewares: request id, security headers, rate limits, JWT auth, bans."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Middleware

from api.auth import AuthError, TokenService
from logging_setup import request_id_var
from services.container import Services
from services.metrics import API_LATENCY, API_REQUESTS
from services.security import SlidingWindowLimiter

logger = logging.getLogger("api")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

API_PREFIX = "/api/v1"
SERVICES_KEY = web.AppKey("services", Services)
TOKENS_KEY = web.AppKey("tokens", TokenService)
LIMITS_KEY = web.AppKey("limits", dict)
# Set by the middleware for each request; typed so a wrong key is a mypy error.
CLIENT_IP_KEY = web.RequestKey("client_ip", str)
TELEGRAM_ID_KEY = web.RequestKey("telegram_id", int)
SESSION_ID_KEY = web.RequestKey[str | None]("session_id")
USER_KEY = web.RequestKey[dict[str, Any]]("user")

PUBLIC_PATHS = frozenset(
    {
        "/auth/telegram",
        "/auth/email/request",
        "/auth/email/verify",
        "/auth/refresh",
        "/legal/offer",
        "/legal/privacy",
        "/push/vapid-key",
    }
)
# Subscription downloads by VPN apps (entry sharding): no Bearer token, the
# panel UUID in the path is the credential, as on Hiddify's own address.
SUB_PREFIX = "/sub/"
AUTH_PATHS = frozenset({"/auth/telegram", "/auth/email/request", "/auth/email/verify"})
BANNED_ALLOWED = frozenset({"/me", "/auth/logout"})

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


def build_limiters(settings: Any = None) -> dict[str, SlidingWindowLimiter]:
    sub_ip = getattr(settings, "sub_rate_per_ip_10min", 30)
    sub_user = getattr(settings, "sub_rate_per_user_hour", 20)
    sub_miss = getattr(settings, "sub_misses_per_ip_hour", 10)
    return {
        # /sub: anti-parsing. Per address, per subscription, and failed guesses
        # (an unknown UUID) per address, which locks the address out for a while.
        "sub_ip": SlidingWindowLimiter(sub_ip, 600),
        "sub_user": SlidingWindowLimiter(sub_user, 3600),
        "sub_miss_ip": SlidingWindowLimiter(sub_miss, 3600),
        "auth_ip": SlidingWindowLimiter(10, 60),
        "public_ip": SlidingWindowLimiter(30, 60),
        "user": SlidingWindowLimiter(60, 60),
        "config_user": SlidingWindowLimiter(10, 60),
        # Each call sends a message to the user's chat.
        "bot_message_user": SlidingWindowLimiter(5, 60),
        # Each code is an email somebody receives: few per address and per sender.
        "email_code_ip": SlidingWindowLimiter(5, 3600),
        "email_code_address": SlidingWindowLimiter(5, 3600),
    }


def json_error(
    status: int, error: str, headers: dict[str, str] | None = None
) -> web.Response:
    return web.json_response({"error": error}, status=status, headers=headers)


def unauthorized() -> web.Response:
    return json_error(401, "unauthorized", {"WWW-Authenticate": "Bearer"})


def client_ip(request: web.Request, trust_forwarded: bool) -> str:
    if trust_forwarded:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # The right-most entry is the one appended by our own reverse proxy.
            return forwarded.split(",")[-1].strip()
    return request.remote or "unknown"


def relative_path(request: web.Request) -> str:
    path = request.path
    return path[len(API_PREFIX) :] if path.startswith(API_PREFIX) else path


def _rate_limited(retry_after: float) -> web.Response:
    return json_error(
        429, "rate_limited", {"Retry-After": str(max(1, int(retry_after) + 1))}
    )


@web.middleware
async def context_middleware(
    request: web.Request, handler: Handler
) -> web.StreamResponse:
    incoming = request.headers.get("X-Request-ID", "")
    request_id = (
        incoming
        if 8 <= len(incoming) <= 64 and incoming.isalnum()
        else uuid.uuid4().hex
    )
    token = request_id_var.set(request_id)
    route = relative_path(request)
    started = time.perf_counter()
    try:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = json_error(exc.status, exc.reason.lower().replace(" ", "_"))
        except Exception:
            logger.exception("api_unhandled_error", extra={"route": route})
            response = json_error(500, "internal_error")
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        response.headers["X-Request-ID"] = request_id
        route_label = (
            request.match_info.route.resource.canonical
            if request.match_info.route.resource
            else "unmatched"
        )
        API_REQUESTS.labels(route=route_label, status=str(response.status)).inc()
        API_LATENCY.labels(route=route_label).observe(time.perf_counter() - started)
        return response
    finally:
        request_id_var.reset(token)


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    if request.method == "OPTIONS":
        return await handler(request)
    services = request.app[SERVICES_KEY]
    limits: dict[str, SlidingWindowLimiter] = request.app[LIMITS_KEY]
    path = relative_path(request)
    ip = client_ip(request, services.settings.trust_forwarded_for)
    request[CLIENT_IP_KEY] = ip

    if path.startswith(SUB_PREFIX):
        # Throttled here and again in the handler (per subscription, misses).
        if not limits["sub_ip"].hit(ip):
            return _rate_limited(limits["sub_ip"].retry_after(ip))
        return await handler(request)

    if path in PUBLIC_PATHS:
        limiter = limits["auth_ip"] if path in AUTH_PATHS else limits["public_ip"]
        if not limiter.hit(ip):
            return _rate_limited(limiter.retry_after(ip))
        return await handler(request)

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        if not limits["public_ip"].hit(ip):
            return _rate_limited(limits["public_ip"].retry_after(ip))
        return unauthorized()
    tokens = request.app[TOKENS_KEY]
    try:
        claims = tokens.decode(header[7:].strip(), "access")
    except AuthError:
        return unauthorized()
    if not await tokens.session_active(claims.get("sid")):
        return unauthorized()
    telegram_id = int(claims["sub"])
    key = str(telegram_id)
    if not limits["user"].hit(key):
        return _rate_limited(limits["user"].retry_after(key))

    user = await services.db.get_user(telegram_id)
    if user is None:
        return unauthorized()
    if user["status"] == "banned" and path not in BANNED_ALLOWED:
        return json_error(403, "banned")
    request[TELEGRAM_ID_KEY] = telegram_id
    request[SESSION_ID_KEY] = claims.get("sid")
    request[USER_KEY] = user
    return await handler(request)


def cors_middleware_factory(origins: list[str]) -> Middleware:
    allowed = frozenset(origins)

    @web.middleware
    async def cors_middleware(
        request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if request.method == "OPTIONS" and origin in allowed:
            response: web.StreamResponse = web.Response(status=204)
        else:
            response = await handler(request)
        if origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type"
            )
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    return cors_middleware
