"""REST API v1, mounted as an aiohttp sub-application at /api/v1."""

from __future__ import annotations

from collections.abc import AsyncIterator

from aiohttp import web
from aiohttp.typedefs import Middleware

from api.auth import ReplayGuard, TokenService
from api.middleware import (
    API_PREFIX,
    LIMITS_KEY,
    SERVICES_KEY,
    TOKENS_KEY,
    auth_middleware,
    build_limiters,
    context_middleware,
    cors_middleware_factory,
)
from api.push import routes as push_routes
from api.routes import REPLAY_KEY, SUB_FANOUT_KEY, routes
from services.container import Services
from services.sharding import FanoutGuard


def create_api_app(services: Services) -> web.Application:
    settings = services.settings
    middlewares: list[Middleware] = [context_middleware]
    if settings.cors_origins:
        middlewares.append(cors_middleware_factory(settings.cors_origins))
    middlewares.append(auth_middleware)
    api = web.Application(middlewares=middlewares, client_max_size=64 * 1024)
    api[SERVICES_KEY] = services
    api[TOKENS_KEY] = TokenService(
        services.db,
        settings.jwt_secret.get_secret_value(),
        settings.jwt_access_ttl_seconds,
        settings.jwt_refresh_ttl_seconds,
    )
    limits = build_limiters(settings)
    api[LIMITS_KEY] = limits
    services.limiters.extend(limits.values())
    api[SUB_FANOUT_KEY] = FanoutGuard(settings.sub_sources_per_user_day, 86400)
    guard = ReplayGuard()
    api[REPLAY_KEY] = guard

    async def replay_guard_ctx(_: web.Application) -> AsyncIterator[None]:
        guard.start(60.0)
        yield
        await guard.stop()

    api.cleanup_ctx.append(replay_guard_ctx)
    api.add_routes(routes)
    api.add_routes(push_routes)
    return api


def register_api_routes(app: web.Application, services: Services) -> web.Application:
    api = create_api_app(services)
    app.add_subapp(API_PREFIX, api)
    return api


__all__ = ["create_api_app", "register_api_routes"]
