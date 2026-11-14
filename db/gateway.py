"""Database gateway: serves the ``Database`` API next to the database file.

The bot may run on another server than its data. This service owns the
SQLite/SQLCipher file and exposes the public ``Database`` methods over a small
JSON protocol; ``db.remote.RemoteDatabase`` is the client. No SQL travels over
the wire and private helpers are not reachable: only named public coroutine
methods with ``int``/``str``/``None`` arguments.

    POST /v1/call   {"id": "<request id>", "method": "get_user", "args": {...}}
    GET  /v1/info   backend facts for the client and the doctor
    GET  /v1/backup the whole database as one file (same encryption), for backups
    GET  /healthz   liveness, no authentication, no details

Every other request carries ``Authorization: Bearer <token>``. Traffic is plain
HTTP, so the service refuses to listen anywhere but a loopback or private
address: it is meant for a WireGuard tunnel between the two servers.

While the token is being replaced, a second one is accepted next to it
(``db_gateway_token_next``): the bot switches over at its own pace, the old
token is dropped only after nobody uses it any more, and a failed switch can go
back (docs/RU_DATABASE_CUTOVER.md, "Смена токена"). ``/v1/info`` says how many
tokens are accepted and when each was last used for real work.

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
import tempfile
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from db.database import Database, DatabaseKeyError, PendingPaymentLimitError
from db.endpoint import MIN_TOKEN_LENGTH, PROTOCOL_VERSION, is_private_host

logger = logging.getLogger("db.gateway")

MAX_BODY_BYTES = 256 * 1024
BACKUP_CHUNK_BYTES = 256 * 1024
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


def api_shapes() -> dict[str, str]:
    """Each remote method's parameters, in a form both sides can compare.

    ``telegram_id,username,referred_by``; ``?`` marks a parameter with a
    default, ``*`` a keyword-only one. Annotations are left out: their text
    differs between Python versions and they do not change what a call needs.
    """
    shapes: dict[str, str] = {}
    for name, signature in remote_methods().items():
        parts = []
        for parameter in list(signature.parameters.values())[1:]:
            mark = "*" if parameter.kind is inspect.Parameter.KEYWORD_ONLY else ""
            optional = "" if parameter.default is inspect.Parameter.empty else "?"
            parts.append(f"{mark}{parameter.name}{optional}")
        shapes[name] = ",".join(parts)
    return shapes


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
    # Exists only while the token is being replaced (see the module docstring).
    db_gateway_token_next: SecretStr | None = None
    db_gateway_host: str
    db_gateway_port: int = Field(8444, ge=1024, le=65535)
    # A missing file is a wrong path far more often than a first installation,
    # and an empty database would look like every user had vanished.
    db_gateway_create: bool = False
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

    @field_validator("db_gateway_token_next", mode="before")
    @classmethod
    def _blank_next_is_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("db_gateway_token_next")
    @classmethod
    def _next_is_long_and_new(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if value is None:
            return None
        if len(value.get_secret_value()) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"db_gateway_token_next must be at least {MIN_TOKEN_LENGTH} characters"
            )
        current = info.data.get("db_gateway_token")
        if current is not None and hmac.compare_digest(
            value.get_secret_value().encode(), current.get_secret_value().encode()
        ):
            raise ValueError("db_gateway_token_next must differ from db_gateway_token")
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


class _TokenUse:
    """How often and how lately a token did real work (not introspection)."""

    def __init__(self) -> None:
        self.calls = 0
        self._last: float | None = None

    def record(self) -> None:
        self.calls += 1
        self._last = time.monotonic()

    def facts(self) -> dict[str, int | None]:
        idle = None if self._last is None else int(time.monotonic() - self._last)
        return {"calls": self.calls, "idle_seconds": idle}


def create_gateway_app(
    database: Database, token: str, next_token: str | None = None
) -> web.Application:
    """The gateway application around an already connected ``Database``.

    ``next_token`` is accepted next to ``token`` while the token is replaced.
    """
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"token must be at least {MIN_TOKEN_LENGTH} characters")
    if next_token is not None:
        if len(next_token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"next token must be at least {MIN_TOKEN_LENGTH} characters"
            )
        if hmac.compare_digest(next_token.encode(), token.encode()):
            raise ValueError("next token must differ from the token")
    methods = remote_methods()
    shapes = api_shapes()
    replay = _Replay()
    running: set[asyncio.Task[None]] = set()
    started = time.monotonic()
    accepted = {"current": f"Bearer {token}".encode()}
    if next_token is not None:
        accepted["next"] = f"Bearer {next_token}".encode()
    uses = {name: _TokenUse() for name in accepted}

    @web.middleware
    async def authenticate(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if request.path == "/healthz":
            return await handler(request)
        given = request.headers.get("Authorization", "").encode()
        who: str | None = None
        for name, wanted in accepted.items():
            # Every candidate is compared: the answer time does not say which.
            if hmac.compare_digest(given, wanted):
                who = name
        if who is None:
            return _error(401, "unauthorized")
        try:
            return await handler(request)
        finally:
            # Recorded after the answer, so /v1/info does not count itself, and
            # only work counts: asking how the tokens are used is not using one.
            if request.path != "/v1/info":
                uses[who].record()

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
                    "api": shapes,
                    "tokens_accepted": len(accepted),
                    "uptime_seconds": int(time.monotonic() - started),
                    "token_use": {name: use.facts() for name, use in uses.items()},
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

    backup_lock = asyncio.Lock()

    async def backup(request: web.Request) -> web.StreamResponse:
        """The whole database as one file, for the nightly backup on the bot's server.

        The same token already reads every record call by call; this only saves
        the round trips. The copy is made next to the database and removed after
        it is sent.
        """
        folder = (
            Path(tempfile.gettempdir())
            if database.path == ":memory:"
            else Path(database.path).parent
        )
        copy = folder / f".backup-{uuid.uuid4().hex}.db"

        def digest_and_size() -> tuple[str, int]:
            digest = hashlib.sha256()
            with copy.open("rb") as source:
                while chunk := source.read(BACKUP_CHUNK_BYTES):
                    digest.update(chunk)
            return digest.hexdigest(), copy.stat().st_size

        async with backup_lock:
            try:
                await database._backup_into(str(copy))
                checksum, size = await asyncio.to_thread(digest_and_size)
                response = web.StreamResponse(
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                        "X-Content-SHA256": checksum,
                    }
                )
                await response.prepare(request)
                with copy.open("rb") as source:
                    while chunk := await asyncio.to_thread(
                        source.read, BACKUP_CHUNK_BYTES
                    ):
                        await response.write(chunk)
                await response.write_eof()
                return response
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                logger.exception(
                    "gateway_backup_failed", extra={"failure": type(exc).__name__}
                )
                return _error(500, "backup_failed")
            finally:
                copy.unlink(missing_ok=True)

    async def finish_running(_app: web.Application) -> None:
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_BODY_BYTES)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/info", info)
    app.router.add_get("/v1/backup", backup)
    app.router.add_post("/v1/call", call)
    app.on_shutdown.append(finish_running)
    return app


class DatabaseFileMissingError(RuntimeError):
    pass


async def serve(settings: GatewaySettings) -> None:
    path = settings.database_path
    if (
        path != ":memory:"
        and not settings.db_gateway_create
        and not (Path(path).is_file() and Path(path).stat().st_size > 0)
    ):
        raise DatabaseFileMissingError(
            f"{path} does not exist or is empty. Put the database file there, "
            "or set DB_GATEWAY_CREATE=true to start with a new empty database"
        )
    database = Database(
        settings.database_path,
        settings.db_key.get_secret_value() if settings.db_key else None,
        activity_logging_enabled=settings.activity_logging_enabled,
    )
    await database.connect()
    next_token = settings.db_gateway_token_next
    app = create_gateway_app(
        database,
        settings.db_gateway_token.get_secret_value(),
        next_token.get_secret_value() if next_token is not None else None,
    )
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
        if next_token is not None:
            # One line per start: a rotation that was opened and forgotten.
            logger.warning("gateway_two_tokens")
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
    except (DatabaseKeyError, DatabaseFileMissingError) as exc:
        sys.stderr.write(f"Database error, refusing to start: {exc}\n")
        raise SystemExit(2) from None
    except OSError as exc:
        # Typically the listening address: the tunnel is not up yet, or the
        # port is taken. One line for the journal; systemd starts us again.
        sys.stderr.write(
            f"Gateway cannot start ({type(exc).__name__}): "
            f"{exc.strerror or 'I/O error'}\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
