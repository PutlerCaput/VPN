"""Database gateway: serves the ``Database`` API next to the database file.

The bot may run on another server than its data. This service owns the
SQLite/SQLCipher file and exposes the public ``Database`` methods over a small
JSON protocol; ``db.remote.RemoteDatabase`` is the client. No SQL travels over
the wire and private helpers are not reachable: only named public coroutine
methods with ``int``/``str``/``None`` arguments.

    POST /v1/call   {"id": "<request id>", "method": "get_user", "args": {...}}
    GET  /v1/info   backend facts for the client and the doctor
    GET  /healthz   liveness, no authentication, no details

Every other request carries ``Authorization: Bearer <token>``. Traffic is plain
HTTP, so the service refuses to listen anywhere but a loopback or private
address: it is meant for a WireGuard tunnel between the two servers.

A repeated request id returns the first answer instead of running the method
again, so a client that lost a response can retry a write safely. A method
keeps running when its caller disconnects, so the answer is there for the
retry. The memory of answered ids lives in this process: a retry that straddles
a restart of the gateway runs the method again (docs/RU_DATABASE_PLAN.md).

Run: ``python -m db.gateway`` (settings from the environment and SECRETS_DIR).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from db.database import Database, DatabaseKeyError, PendingPaymentLimitError
from db.endpoint import MIN_TOKEN_LENGTH, PROTOCOL_VERSION, is_private_host

logger = logging.getLogger("db.gateway")

MAX_BODY_BYTES = 256 * 1024
# Answers kept for replay: enough for every in-flight retry, small enough for 1 GB.
REPLAY_ENTRIES = 4096
REPLAY_TTL_SECONDS = 600
REPLAY_MAX_BODY_BYTES = 64 * 1024
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Exceptions the client re-raises as the same type, most specific first.
# Anything else is an internal error reported by type name only.
DOMAIN_ERRORS: tuple[type[Exception], ...] = (
    PendingPaymentLimitError,
    sqlite3.IntegrityError,
    ValueError,
    RuntimeError,
)


def domain_error(exc: Exception) -> str | None:
    """The wire name of an exception callers may handle, or None."""
    for kind in DOMAIN_ERRORS:
        if isinstance(exc, kind):
            return kind.__name__
    # SQLCipher's driver has its own exception classes with the same names.
    if any(kind.__name__ == "IntegrityError" for kind in type(exc).__mro__):
        return "IntegrityError"
    return None


# Lifecycle members are local to each side, not part of the remote API.
LOCAL_ONLY = frozenset({"connect", "close"})

Answer = tuple[int, bytes]


def remote_methods() -> dict[str, inspect.Signature]:
    """Public coroutine methods of ``Database`` and their signatures."""
    methods: dict[str, inspect.Signature] = {}
    for name, member in inspect.getmembers(Database, inspect.iscoroutinefunction):
        if name.startswith("_") or name in LOCAL_ONLY:
            continue
        methods[name] = inspect.signature(member)
    return methods


def _secrets_dir() -> str | None:
    value = os.environ.get("SECRETS_DIR", "/run/secrets")
    return value if Path(value).is_dir() else None


class GatewaySettings(BaseSettings):
    """Read from the environment and from SECRETS_DIR, like the bot's settings."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        secrets_dir=_secrets_dir(),
    )

    database_path: str = "data/bot.db"
    db_key: SecretStr | None = None
    db_gateway_token: SecretStr
    db_gateway_host: str
    db_gateway_port: int = Field(8444, ge=1024, le=65535)
    activity_logging_enabled: bool = False
    log_level: str = "INFO"

    @field_validator("db_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("db_gateway_token", mode="before")
    @classmethod
    def _strip_token(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("db_gateway_token")
    @classmethod
    def _long_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"db_gateway_token must be at least {MIN_TOKEN_LENGTH} characters"
            )
        return value

    @field_validator("db_gateway_host")
    @classmethod
    def _private_listener(cls, value: str) -> str:
        if not is_private_host(value):
            raise ValueError(
                "db_gateway_host must be a loopback or private address: the "
                "gateway speaks plain HTTP and belongs inside the server tunnel"
            )
        return value


class _Replay:
    """Answers by request id: a retry gets the first answer, not a second run."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, tuple[float, str, asyncio.Future[Answer]]] = (
            OrderedDict()
        )

    def _evict(self, at: float) -> None:
        # Entries are in arrival order, so the oldest is first. An answer that
        # is still being computed is never dropped: it moves to the back.
        for _ in range(len(self._entries)):
            key, (expires, _digest, future) = next(iter(self._entries.items()))
            if expires > at and len(self._entries) <= REPLAY_ENTRIES:
                return
            if future.done():
                del self._entries[key]
            else:
                self._entries.move_to_end(key)

    def get(self, request_id: str) -> tuple[str, asyncio.Future[Answer]] | None:
        self._evict(time.monotonic())
        entry = self._entries.get(request_id)
        return None if entry is None else (entry[1], entry[2])

    def open(self, request_id: str, digest: str) -> asyncio.Future[Answer]:
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._entries[request_id] = (
            time.monotonic() + REPLAY_TTL_SECONDS,
            digest,
            future,
        )
        return future

    def forget(self, request_id: str) -> None:
        self._entries.pop(request_id, None)


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _reply(status: int, body: bytes, *, replayed: bool = False) -> web.Response:
    return web.Response(
        status=status,
        body=body,
        content_type="application/json",
        headers={"X-Replayed": "1"} if replayed else None,
    )


def _error(status: int, code: str) -> web.Response:
    return _reply(status, _json({"error": {"type": code, "message": code}}))


def create_gateway_app(database: Database, token: str) -> web.Application:
    """The gateway application around an already connected ``Database``."""
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"token must be at least {MIN_TOKEN_LENGTH} characters")
    methods = remote_methods()
    replay = _Replay()
    running: set[asyncio.Task[None]] = set()
    expected = f"Bearer {token}".encode()

    @web.middleware
    async def authenticate(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if request.path != "/healthz":
            given = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                return _error(401, "unauthorized")
        return await handler(request)

    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def info(_request: web.Request) -> web.Response:
        try:
            ok = await database.ping()
        except Exception:
            logger.exception("gateway_ping_failed")
            ok = False
        return _reply(
            200 if ok else 503,
            _json(
                {
                    "ok": ok,
                    "protocol": PROTOCOL_VERSION,
                    "encrypted": database.encrypted,
                    "activity_logging_enabled": database.activity_logging_enabled,
                    "server_time": int(time.time()),
                    "methods": len(methods),
                }
            ),
        )

    async def run(method: str, args: dict[str, Any]) -> Answer:
        try:
            result = await getattr(database, method)(**args)
        except Exception as exc:
            name = domain_error(exc)
            if name is not None:
                return 200, _json({"error": {"type": name, "message": str(exc)}})
            # The message may quote a value: the log and the client get the
            # exception type and the method name only.
            logger.exception(
                "gateway_call_failed",
                extra={"failure": type(exc).__name__, "reason": method},
            )
            return 500, _json(
                {"error": {"type": type(exc).__name__, "message": "internal error"}}
            )
        try:
            return 200, _json({"result": result})
        except (TypeError, ValueError):
            # Still an answer: it is kept, so a retry cannot run the method twice.
            logger.error("gateway_result_not_json", extra={"reason": method})
            return 500, _json(
                {"error": {"type": "ResultNotJson", "message": "internal error"}}
            )

    async def execute(
        request_id: str,
        method: str,
        args: dict[str, Any],
        future: asyncio.Future[Answer],
    ) -> None:
        try:
            answer = await run(method, args)
        except BaseException:
            # Shutdown: nothing to replay; waiters are cancelled with us.
            replay.forget(request_id)
            future.cancel()
            raise
        future.set_result(answer)
        if len(answer[1]) > REPLAY_MAX_BODY_BYTES:
            # Only reads are this large; running one again is harmless.
            replay.forget(request_id)

    async def call(request: web.Request) -> web.Response:
        raw = await request.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            return _error(400, "invalid_json")
        if not isinstance(payload, dict):
            return _error(400, "invalid_request")
        request_id = payload.get("id")
        method = payload.get("method")
        args = payload.get("args", {})
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            return _error(400, "invalid_request_id")
        if not isinstance(method, str) or not isinstance(args, dict):
            return _error(400, "invalid_request")
        signature = methods.get(method)
        if signature is None:
            return _error(404, "unknown_method")
        for value in args.values():
            # bool is an int in Python; the API takes no booleans.
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int | str)
            ):
                return _error(400, "invalid_argument_type")
        try:
            signature.bind(database, **args)
        except TypeError:
            return _error(400, "invalid_arguments")

        digest = hashlib.sha256(raw).hexdigest()
        earlier = replay.get(request_id)
        if earlier is not None:
            known_digest, answered = earlier
            if not hmac.compare_digest(known_digest, digest):
                return _error(409, "request_id_reused")
            status, body = await asyncio.shield(answered)
            return _reply(status, body, replayed=True)

        future = replay.open(request_id, digest)
        # A separate task: the method finishes and its answer is kept even if
        # this caller goes away before reading it.
        task = asyncio.create_task(execute(request_id, method, args, future))
        running.add(task)
        task.add_done_callback(running.discard)
        status, body = await asyncio.shield(future)
        return _reply(status, body)

    async def finish_running(_app: web.Application) -> None:
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_BODY_BYTES)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/info", info)
    app.router.add_post("/v1/call", call)
    app.on_shutdown.append(finish_running)
    return app


async def serve(settings: GatewaySettings) -> None:
    database = Database(
        settings.database_path,
        settings.db_key.get_secret_value() if settings.db_key else None,
        activity_logging_enabled=settings.activity_logging_enabled,
    )
    await database.connect()
    app = create_gateway_app(database, settings.db_gateway_token.get_secret_value())
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.db_gateway_host, settings.db_gateway_port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await site.start()
        logger.info("gateway_started")
        await stop.wait()
    finally:
        await runner.cleanup()
        await database.close()
        logger.info("gateway_stopped")


def main() -> None:
    from logging_setup import setup_logging

    try:
        settings = GatewaySettings()
    except ValidationError as exc:
        # Field names only: a validation error may echo a secret value.
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        sys.stderr.write(
            f"Gateway configuration error, refusing to start: {', '.join(fields)}\n"
        )
        raise SystemExit(2) from None
    setup_logging(settings.log_level, None)
    try:
        asyncio.run(serve(settings))
    except DatabaseKeyError as exc:
        sys.stderr.write(f"Database error, refusing to start: {exc}\n")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
