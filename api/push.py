"""Web Push subscription endpoints."""

from __future__ import annotations

from aiohttp import web
from pydantic import ValidationError

from api.middleware import SERVICES_KEY, json_error
from api.schemas import PushSubscribeIn, PushUnsubscribeIn

routes = web.RouteTableDef()


@routes.get("/push/vapid-key")
async def vapid_key(request: web.Request) -> web.Response:
    push = request.app[SERVICES_KEY].push
    if not push.enabled:
        return json_error(404, "push_disabled")
    return web.json_response({"public_key": push.public_key})


@routes.post("/push/subscribe")
async def subscribe(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    if not services.push.enabled:
        return json_error(404, "push_disabled")
    try:
        body = PushSubscribeIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        return json_error(400, "invalid_body")
    endpoint = str(body.endpoint)
    if not endpoint.startswith("https://"):
        return json_error(400, "invalid_endpoint")
    await services.db.upsert_push_subscription(
        request["telegram_id"],
        endpoint,
        body.keys.p256dh,
        body.keys.auth,
    )
    return web.json_response({"ok": True}, status=201)


@routes.delete("/push/subscribe")
async def unsubscribe(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    endpoint: str | None = None
    if request.can_read_body:
        try:
            body = PushUnsubscribeIn.model_validate(await request.json())
            endpoint = str(body.endpoint) if body.endpoint else None
        except (ValueError, ValidationError):
            return json_error(400, "invalid_body")
    removed = await services.db.delete_push_subscription(
        request["telegram_id"], endpoint
    )
    return web.json_response({"removed": removed})
