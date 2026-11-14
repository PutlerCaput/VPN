"""The database gateway and its client: the bot's data on another server."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, unused_port
from pydantic import ValidationError

import main
from config import Settings
from db.database import Database, PendingPaymentLimitError
from db.endpoint import PROTOCOL_VERSION, is_private_host, validate_gateway_url
from db.gateway import (
    REPLAY_ENTRIES,
    GatewaySettings,
    api_shapes,
    create_gateway_app,
    remote_methods,
)
from db.remote import (
    DatabaseUnavailableError,
    RemoteDatabase,
    RemoteDatabaseError,
)
from tests.conftest import GATEWAY_TOKEN

AUTH = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def local() -> AsyncIterator[Database]:
    database = Database(":memory:", os.environ.get("TEST_DB_KEY") or None)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def gateway(local: Database) -> AsyncIterator[TestServer]:
    server = TestServer(create_gateway_app(local, GATEWAY_TOKEN))
    await server.start_server()
    yield server
    await server.close()


@pytest.fixture
async def remote(gateway: TestServer) -> AsyncIterator[RemoteDatabase]:
    client = RemoteDatabase(str(gateway.make_url("/")), GATEWAY_TOKEN)
    await client.connect()
    yield client
    await client.close()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def call_body(method: str, args: dict[str, Any] | None = None, **extra: Any) -> bytes:
    payload = {"id": uuid.uuid4().hex, "method": method, "args": args or {}, **extra}
    return json.dumps(payload).encode()


async def post(
    http: aiohttp.ClientSession,
    gateway: TestServer,
    body: bytes,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], bool]:
    async with http.post(
        gateway.make_url("/v1/call"),
        data=body,
        headers=AUTH if headers is None else headers,
    ) as response:
        return (
            response.status,
            await response.json(),
            response.headers.get("X-Replayed") == "1",
        )


# ------------------------------------------------------------- the surface
def test_every_public_method_is_forwarded() -> None:
    methods = remote_methods()
    assert len(methods) > 100
    for name in methods:
        forwarder = RemoteDatabase.__dict__.get(name)
        assert forwarder is not None, f"{name} would run local SQL"
        assert forwarder is not Database.__dict__[name]
    # Nothing else that touches the file is inherited.
    for name, _member in inspect.getmembers(Database, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert name in RemoteDatabase.__dict__, name
    for name in ("conn", "encrypted"):
        assert name in RemoteDatabase.__dict__


def test_lifecycle_and_private_helpers_are_not_remote() -> None:
    methods = remote_methods()
    for name in ("connect", "close", "transaction", "_execute", "_fetchone", "conn"):
        assert name not in methods


async def test_health_needs_no_token_everything_else_does(
    gateway: TestServer, http: aiohttp.ClientSession
) -> None:
    async with http.get(gateway.make_url("/healthz")) as response:
        assert response.status == 200
        assert await response.text() == "ok"
    for headers in (
        {},
        {"Authorization": "Bearer " + "x" * 40},
        {"Authorization": GATEWAY_TOKEN},
    ):
        async with http.get(gateway.make_url("/v1/info"), headers=headers) as response:
            assert response.status == 401
        status, answer, _ = await post(http, gateway, call_body("ping"), headers)
        assert status == 401
        assert answer["error"]["type"] == "unauthorized"


@pytest.mark.parametrize(
    "method",
    ["_execute", "_fetchall", "connect", "close", "transaction", "conn", "nope", ""],
)
async def test_only_named_public_methods_can_be_called(
    gateway: TestServer, http: aiohttp.ClientSession, method: str
) -> None:
    status, answer, _ = await post(http, gateway, call_body(method))
    assert status == 404
    assert answer["error"]["type"] == "unknown_method"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"telegram_id": True}, "invalid_argument_type"),
        ({"telegram_id": 1.5}, "invalid_argument_type"),
        ({"telegram_id": [1]}, "invalid_argument_type"),
        ({"telegram_id": {"$ne": 1}}, "invalid_argument_type"),
        ({}, "invalid_arguments"),
        ({"telegram_id": 1, "sql": "DROP TABLE users"}, "invalid_arguments"),
        ({"self": 1, "telegram_id": 1}, "invalid_arguments"),
    ],
)
async def test_arguments_are_checked_before_the_call(
    gateway: TestServer, http: aiohttp.ClientSession, args: dict[str, Any], code: str
) -> None:
    status, answer, _ = await post(http, gateway, call_body("get_user", args))
    assert status == 400
    assert answer["error"]["type"] == code


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not json", "invalid_json"),
        (b"[1, 2]", "invalid_request"),
        (b'{"id": "abc", "method": "ping", "args": {}}', "invalid_request_id"),
        (b'{"method": "ping", "args": {}}', "invalid_request_id"),
        (json.dumps({"id": "A" * 32, "method": "ping"}).encode(), "invalid_request_id"),
        (json.dumps({"id": "a" * 32, "method": 7}).encode(), "invalid_request"),
        (
            json.dumps({"id": "a" * 32, "method": "ping", "args": []}).encode(),
            "invalid_request",
        ),
    ],
)
async def test_malformed_requests_are_refused(
    gateway: TestServer, http: aiohttp.ClientSession, body: bytes, code: str
) -> None:
    status, answer, _ = await post(http, gateway, body)
    assert status == 400
    assert answer["error"]["type"] == code


async def test_oversized_request_is_refused(
    gateway: TestServer, http: aiohttp.ClientSession
) -> None:
    body = call_body("set_receipt_email", {"telegram_id": 1, "email": "a" * 300_000})
    async with http.post(
        gateway.make_url("/v1/call"), data=body, headers=AUTH
    ) as response:
        assert response.status == 413


# ----------------------------------------------------------- the round trip
async def test_calls_return_what_the_local_database_returns(
    remote: RemoteDatabase, local: Database
) -> None:
    assert await remote.ping() is True
    assert await remote.get_user(42) is None
    assert await remote.create_user(42, "имя", None) is True
    assert await remote.create_user(42, "again", None) is False
    user = await remote.get_user(42)
    assert user == await local.get_user(42)
    assert user is not None
    assert user["username"] == "имя"
    # Positional and keyword arguments both work, as on Database.
    assert await remote.get_user(telegram_id=42) == user
    with pytest.raises(TypeError):
        await remote.get_user()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await remote.get_user(42, 43)  # type: ignore[call-arg]


async def test_rows_declared_as_tuples_come_back_as_tuples(
    remote: RemoteDatabase, local: Database
) -> None:
    await remote.add_server_traffic("2026-11-01", 5)
    await remote.add_server_traffic("2026-11-02", 7)
    rows = await remote.server_traffic_since("2026-11-01")
    assert rows == await local.server_traffic_since("2026-11-01")
    assert rows == [("2026-11-01", 5), ("2026-11-02", 7)]
    assert all(isinstance(row, tuple) for row in rows)


async def test_booleans_cross_as_integers_and_other_types_never_leave(
    remote: RemoteDatabase, local: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    async def fake_get_user(telegram_id: int) -> None:
        seen.append(telegram_id)

    monkeypatch.setattr(local, "get_user", fake_get_user)
    await remote.get_user(True)  # type: ignore[arg-type]
    assert seen == [1]
    assert type(seen[0]) is int
    for value in (1.5, b"x", [1], {"a": 1}, object()):
        with pytest.raises(TypeError, match="int, str or None"):
            await remote.get_user(value)  # type: ignore[arg-type]
    assert seen == [1]


async def test_backend_facts(remote: RemoteDatabase, local: Database) -> None:
    facts = await remote.info()
    assert facts["ok"] is True
    assert facts["protocol"] == PROTOCOL_VERSION
    assert facts["methods"] == len(remote_methods())
    assert facts["api"] == api_shapes()
    assert facts["encrypted"] is local.encrypted
    assert remote.encrypted is local.encrypted
    assert remote.activity_logging_enabled is False
    assert abs(remote.clock_skew_seconds) <= 2
    assert abs(facts["server_time"] - int(time.time())) <= 2


async def test_the_activity_switch_is_the_one_next_to_the_data() -> None:
    database = Database(":memory:", activity_logging_enabled=True)
    await database.connect()
    server = TestServer(create_gateway_app(database, GATEWAY_TOKEN))
    await server.start_server()
    client = RemoteDatabase(str(server.make_url("/")), GATEWAY_TOKEN)
    try:
        await client.connect()
        assert client.activity_logging_enabled is True
    finally:
        await client.close()
        await server.close()
        await database.close()


async def test_there_is_no_local_connection(remote: RemoteDatabase) -> None:
    with pytest.raises(RuntimeError, match="no local connection"):
        _ = remote.conn
    with pytest.raises(RuntimeError, match="Database method"):
        async with remote.transaction():
            pass


async def test_calls_before_connect_fail_clearly(gateway: TestServer) -> None:
    client = RemoteDatabase(str(gateway.make_url("/")), GATEWAY_TOKEN)
    with pytest.raises(RuntimeError, match="not connected"):
        await client.ping()


# ------------------------------------------------------------------- errors
def sqlcipher_integrity_error() -> Exception:
    from sqlcipher3 import dbapi2

    return dbapi2.IntegrityError("UNIQUE constraint failed: payments.payment_id")


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (PendingPaymentLimitError("too many pending"), PendingPaymentLimitError),
        (
            sqlite3.IntegrityError("UNIQUE constraint failed: users.x"),
            sqlite3.IntegrityError,
        ),
        (sqlcipher_integrity_error(), sqlite3.IntegrityError),
        (ValueError("unknown tier"), ValueError),
        (RuntimeError("Database is not connected"), RuntimeError),
    ],
)
async def test_domain_errors_keep_their_type_and_message(
    remote: RemoteDatabase,
    local: Database,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    expected: type[Exception],
) -> None:
    async def failing() -> bool:
        raise raised

    monkeypatch.setattr(local, "ping", failing)
    with pytest.raises(expected) as caught:
        await remote.ping()
    assert type(caught.value) is expected
    assert str(caught.value) == str(raised)


async def test_unexpected_errors_do_not_leak_their_message(
    remote: RemoteDatabase,
    local: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing() -> bool:
        raise KeyError("ivan@example.com")

    monkeypatch.setattr(local, "ping", failing)
    with pytest.raises(RemoteDatabaseError) as caught:
        await remote.ping()
    assert "KeyError" in str(caught.value)
    assert "ivan@example.com" not in str(caught.value)
    record = next(r for r in caplog.records if r.msg == "gateway_call_failed")
    assert record.__dict__["failure"] == "KeyError"
    assert record.__dict__["reason"] == "ping"
    # An unexpected failure is not a network outage: no retry, no cooldown.
    monkeypatch.undo()
    assert await remote.ping() is True


# ------------------------------------------------------- retries and replay
async def test_a_repeated_request_id_gets_the_first_answer(
    gateway: TestServer, http: aiohttp.ClientSession, local: Database
) -> None:
    body = call_body(
        "create_user", {"telegram_id": 7, "username": "a", "referred_by": None}
    )
    first = await post(http, gateway, body)
    second = await post(http, gateway, body)
    assert first == (200, {"result": True}, False)
    # Run again it would answer False: the user already exists.
    assert second == (200, {"result": True}, True)
    fresh = call_body(
        "create_user", {"telegram_id": 7, "username": "a", "referred_by": None}
    )
    assert await post(http, gateway, fresh) == (200, {"result": False}, False)


async def test_a_request_id_cannot_be_reused_for_another_call(
    gateway: TestServer, http: aiohttp.ClientSession
) -> None:
    request_id = uuid.uuid4().hex
    one = json.dumps(
        {"id": request_id, "method": "get_user", "args": {"telegram_id": 1}}
    )
    two = json.dumps(
        {"id": request_id, "method": "get_user", "args": {"telegram_id": 2}}
    )
    assert (await post(http, gateway, one.encode()))[0] == 200
    status, answer, _ = await post(http, gateway, two.encode())
    assert status == 409
    assert answer["error"]["type"] == "request_id_reused"


async def test_a_write_finishes_and_is_not_repeated_when_the_caller_goes_away(
    gateway: TestServer,
    http: aiohttp.ClientSession,
    local: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def slow_write(day: str, delta: int) -> None:
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(local, "add_server_traffic", slow_write)
    body = call_body("add_server_traffic", {"day": "2026-11-01", "delta": 5})
    lost = asyncio.create_task(post(http, gateway, body))
    await started.wait()
    lost.cancel()  # the response is lost: the connection drops mid-call
    with pytest.raises(asyncio.CancelledError):
        await lost
    # The retry arrives while the first run is still in progress...
    retry = asyncio.create_task(post(http, gateway, body))
    await asyncio.sleep(0.05)
    assert not retry.done()
    release.set()
    # ...and receives the answer of that run instead of starting a second one.
    assert await retry == (200, {"result": None}, True)
    assert await post(http, gateway, body) == (200, {"result": None}, True)
    assert runs == 1


async def test_replay_memory_is_bounded(
    gateway: TestServer, http: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import db.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "REPLAY_ENTRIES", 3)
    assert REPLAY_ENTRIES > 3
    bodies = [call_body("ping") for _ in range(6)]
    for body in bodies:
        assert await post(http, gateway, body) == (200, {"result": True}, False)
    # The newest answers are still replayed, the oldest were forgotten.
    assert (await post(http, gateway, bodies[-1]))[2] is True
    assert (await post(http, gateway, bodies[0]))[2] is False


async def test_large_answers_are_not_kept_for_replay(
    gateway: TestServer,
    http: aiohttp.ClientSession,
    local: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def big(telegram_id: int) -> dict[str, Any]:
        return {"blob": "x" * 70_000}

    monkeypatch.setattr(local, "get_user", big)
    body = call_body("get_user", {"telegram_id": 1})
    assert (await post(http, gateway, body))[2] is False
    assert (await post(http, gateway, body))[2] is False


async def test_a_result_that_cannot_be_sent_is_an_error_not_a_second_run(
    gateway: TestServer,
    http: aiohttp.ClientSession,
    local: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = 0

    async def odd(telegram_id: int) -> Any:
        nonlocal runs
        runs += 1
        return {"raw": b"bytes"}

    monkeypatch.setattr(local, "get_user", odd)
    body = call_body("get_user", {"telegram_id": 1})
    status, answer, replayed = await post(http, gateway, body)
    assert (status, answer["error"]["type"], replayed) == (500, "ResultNotJson", False)
    status, answer, replayed = await post(http, gateway, body)
    assert (status, answer["error"]["type"], replayed) == (500, "ResultNotJson", True)
    assert runs == 1


# ------------------------------------------------------------------ backups
async def test_the_whole_database_can_be_fetched_as_one_file(
    remote: RemoteDatabase, local: Database, tmp_path: Any
) -> None:
    await remote.create_user(31, "kept", None)
    destination = tmp_path / "copy.db"
    size = await remote.fetch_backup(str(destination))
    assert size == destination.stat().st_size > 0
    assert not destination.with_name("copy.db.part").exists()
    # The copy opens with the same key (or none) and holds the same rows.
    copy = Database(str(destination), os.environ.get("TEST_DB_KEY") or None)
    await copy.connect()
    try:
        user = await copy.get_user(31)
        assert user is not None
        assert user["username"] == "kept"
    finally:
        await copy.close()
    if local.encrypted:
        assert not destination.read_bytes().startswith(b"SQLite format 3")


async def test_a_backup_needs_the_token_and_leaves_nothing_behind(
    gateway: TestServer, http: aiohttp.ClientSession, tmp_path: Any
) -> None:
    import tempfile

    async with http.get(gateway.make_url("/v1/backup")) as response:
        assert response.status == 401
    before = set(Path(tempfile.gettempdir()).glob(".backup-*.db"))
    async with http.get(gateway.make_url("/v1/backup"), headers=AUTH) as response:
        assert response.status == 200
        body = await response.read()
        assert len(body) == int(response.headers["Content-Length"])
        assert len(response.headers["X-Content-SHA256"]) == 64
    # The gateway removes its copy right after sending it; give it a moment.
    for _ in range(40):
        left = set(Path(tempfile.gettempdir()).glob(".backup-*.db")) - before
        if not left:
            break
        await asyncio.sleep(0.05)
    assert not left


async def test_a_damaged_download_is_not_kept(
    gateway: TestServer, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import db.remote as remote_module

    class WrongDigest:
        def update(self, _chunk: bytes) -> None:
            return None

        def hexdigest(self) -> str:
            return "0" * 64

    client = RemoteDatabase(str(gateway.make_url("/")), GATEWAY_TOKEN)
    await client.connect()
    # The client's view of hashlib only: the gateway in this process keeps the real one.
    monkeypatch.setattr(remote_module, "hashlib", SimpleNamespace(sha256=WrongDigest))
    destination = tmp_path / "copy.db"
    try:
        with pytest.raises(RemoteDatabaseError, match="damaged"):
            await client.fetch_backup(str(destination))
    finally:
        await client.close()
    assert list(tmp_path.iterdir()) == []


async def test_the_database_tool_fetches_a_copy_the_local_key_opens(
    tmp_path: Any,
) -> None:
    """What the nightly backup runs on the bot's server after the move."""
    key = "cd" * 32
    database = Database(str(tmp_path / "live.db"), key)
    await database.connect()
    await database.create_user(55, "nightly", None)
    server = TestServer(create_gateway_app(database, GATEWAY_TOKEN))
    await server.start_server()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "db_key").write_text(key)
    environment = {
        **os.environ,
        "SECRETS_DIR": str(secrets),
        "DATABASE_URL": str(server.make_url("/")),
        "DB_GATEWAY_TOKEN": GATEWAY_TOKEN,
    }

    async def tool(*args: str, **env: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "scripts" / "db_tool.py"),
            *args,
            env={**environment, **env},
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return process.returncode or 0, output.decode()

    destination = tmp_path / "nightly.db"
    try:
        code, output = await tool("fetch", str(destination))
        assert code == 0, output
        assert "backup fetched" in output
        assert GATEWAY_TOKEN not in output
        copy = Database(str(destination), key)
        await copy.connect()
        user = await copy.get_user(55)
        await copy.close()
        assert user is not None
        assert user["username"] == "nightly"
        # A key that cannot open the copy is caught on the day of the backup.
        (secrets / "db_key").write_text("ef" * 32)
        code, output = await tool("fetch", str(tmp_path / "other.db"))
        assert code != 0
        assert "wrong key" in output
        # Without DATABASE_URL there is nothing to fetch from.
        code, output = await tool("fetch", str(tmp_path / "none.db"), DATABASE_URL="")
        assert code != 0
        assert "DATABASE_URL is not set" in output
    finally:
        await server.close()
        await database.close()


def test_the_backup_helper_is_not_callable_remotely() -> None:
    # It takes a path on the server: it must stay out of the remote API.
    assert "_backup_into" not in remote_methods()
    assert not any("backup" in name for name in remote_methods())


class FlakyGateway:
    """Stands in for the gateway: fails the first calls, records every body."""

    def __init__(
        self,
        failures: list[int],
        protocol: int = PROTOCOL_VERSION,
        api: dict[str, str] | None = None,
    ) -> None:
        self.failures = failures
        self.protocol = protocol
        self.api = api_shapes() if api is None else api
        self.bodies: list[dict[str, Any]] = []
        self.app = web.Application()
        self.app.router.add_get("/v1/info", self.info)
        self.app.router.add_post("/v1/call", self.call)

    async def info(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "protocol": self.protocol,
                "encrypted": True,
                "activity_logging_enabled": False,
                "server_time": int(time.time()) + 120,
                "methods": 0,
                "api": self.api,
            }
        )

    async def call(self, request: web.Request) -> web.Response:
        self.bodies.append(await request.json())
        if self.failures:
            return web.Response(status=self.failures.pop(0))
        return web.json_response({"result": 7})


async def test_a_retry_repeats_the_same_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("db.remote.RETRY_DELAYS", (0.0, 0.0))
    flaky = FlakyGateway([503, 502])
    server = TestServer(flaky.app)
    await server.start_server()
    client = RemoteDatabase(str(server.make_url("/")), GATEWAY_TOKEN)
    try:
        await client.connect()
        assert client.encrypted is True
        assert 115 <= client.clock_skew_seconds <= 125
        assert await client.count_active_users() == 7
        assert len(flaky.bodies) == 3
        assert flaky.bodies[0] == flaky.bodies[1] == flaky.bodies[2]
        assert flaky.bodies[0]["method"] == "count_active_users"
        # A new call gets a new id.
        await client.count_active_users()
        assert flaky.bodies[3]["id"] != flaky.bodies[0]["id"]
    finally:
        await client.close()
        await server.close()


async def test_an_outage_fails_fast_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("db.remote.RETRY_DELAYS", (0.0, 0.0))
    flaky = FlakyGateway([503, 503, 503])
    server = TestServer(flaky.app)
    await server.start_server()
    client = RemoteDatabase(str(server.make_url("/")), GATEWAY_TOKEN, cooldown=0.2)
    try:
        await client.connect()
        with pytest.raises(DatabaseUnavailableError):
            await client.count_active_users()
        assert len(flaky.bodies) == 3
        # During the cooldown nobody waits behind timeouts and nothing is sent.
        with pytest.raises(DatabaseUnavailableError):
            await client.count_active_users()
        assert len(flaky.bodies) == 3
        await asyncio.sleep(0.25)
        assert await client.count_active_users() == 7
    finally:
        await client.close()
        await server.close()


async def test_an_unreachable_gateway_is_a_connection_error() -> None:
    client = RemoteDatabase(
        f"http://127.0.0.1:{unused_port()}",
        GATEWAY_TOKEN,
        retries=0,
        connect_timeout=1.0,
    )
    try:
        with pytest.raises(DatabaseUnavailableError) as caught:
            await client.connect()
        assert isinstance(caught.value, ConnectionError)
        assert not isinstance(caught.value, RuntimeError | ValueError | TimeoutError)
    finally:
        await client.close()


async def test_another_protocol_version_is_refused() -> None:
    server = TestServer(FlakyGateway([], protocol=PROTOCOL_VERSION + 1).app)
    await server.start_server()
    client = RemoteDatabase(str(server.make_url("/")), GATEWAY_TOKEN)
    try:
        with pytest.raises(RemoteDatabaseError, match="protocol version"):
            await client.connect()
        assert client._session is None
    finally:
        await client.close()
        await server.close()


def test_api_shapes_name_parameters_without_annotations() -> None:
    shapes = api_shapes()
    assert set(shapes) == set(remote_methods())
    assert shapes["get_user"] == "telegram_id"
    assert shapes["create_user"] == "telegram_id,username,referred_by"
    assert shapes["ping"] == ""
    assert not any("int" in shape.split(",") for shape in shapes.values())


@pytest.mark.parametrize(
    ("change", "accepted"),
    [
        (lambda api: api, True),
        # A newer gateway knows more methods: the bot does not mind.
        (lambda api: {**api, "added_in_a_later_release": "x,y"}, True),
        # An older gateway lacks one, or takes other parameters: refuse.
        (lambda api: {k: v for k, v in api.items() if k != "get_user"}, False),
        (lambda api: {**api, "get_user": "telegram_id,extra"}, False),
        (lambda api: {}, False),
    ],
)
async def test_the_bot_refuses_a_gateway_older_than_itself(
    change: Any, accepted: bool
) -> None:
    server = TestServer(FlakyGateway([], api=change(api_shapes())).app)
    await server.start_server()
    client = RemoteDatabase(str(server.make_url("/")), GATEWAY_TOKEN)
    try:
        if accepted:
            await client.connect()
        else:
            with pytest.raises(RemoteDatabaseError, match="update the gateway first"):
                await client.connect()
            assert client._session is None
    finally:
        await client.close()
        await server.close()


async def test_a_wrong_token_is_not_an_outage(gateway: TestServer) -> None:
    client = RemoteDatabase(str(gateway.make_url("/")), "wrong-token-" + "0" * 30)
    try:
        with pytest.raises(RemoteDatabaseError, match="401"):
            await client.connect()
    finally:
        await client.close()


# ----------------------------------------------------------------- settings
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "10.204.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.10",
        "fd00::1",
    ],
)
def test_private_hosts(host: str) -> None:
    assert is_private_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # noqa: S104
        "::",
        "45.138.157.65",
        "172.32.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "fe80::1",
        "::ffff:10.0.0.1",
        "localhost",
        "db.example.com",
        "",
    ],
)
def test_public_and_ambiguous_hosts_are_not_private(host: str) -> None:
    assert not is_private_host(host)


@pytest.mark.parametrize(
    ("url", "normalised"),
    [
        ("http://10.204.0.1:8444", "http://10.204.0.1:8444"),
        (" http://10.204.0.1:8444/ ", "http://10.204.0.1:8444"),
        ("http://127.0.0.1:8444", "http://127.0.0.1:8444"),
        ("http://[fd00::1]:8444", "http://[fd00::1]:8444"),
        ("https://db.example.com", "https://db.example.com"),
        ("https://db.example.com:8444/", "https://db.example.com:8444"),
    ],
)
def test_gateway_urls_accepted(url: str, normalised: str) -> None:
    assert validate_gateway_url(url) == normalised


@pytest.mark.parametrize(
    "url",
    [
        "http://45.138.157.65:8444",
        "http://db.example.com:8444",
        "http://10.204.0.1",
        "http://0.0.0.0:8444",
        "ftp://10.204.0.1:8444",
        "10.204.0.1:8444",
        "http://user:secret@10.204.0.1:8444",
        "http://10.204.0.1:8444/v1/call",
        "http://10.204.0.1:8444/?a=1",
        "http://10.204.0.1:8444/#x",
        "http://10.204.0.1:port",
        "https://",
        "",
    ],
)
def test_gateway_urls_refused(url: str) -> None:
    with pytest.raises(ValueError, match="database_url"):
        validate_gateway_url(url)


def test_bot_settings_need_a_token_with_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "http://10.204.0.1:8444/")
    with pytest.raises(ValidationError, match="db_gateway_token"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("DB_GATEWAY_TOKEN", "short")
    with pytest.raises(ValidationError, match="db_gateway_token") as caught:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "short" not in str(caught.value.errors(include_input=False))
    monkeypatch.setenv("DB_GATEWAY_TOKEN", GATEWAY_TOKEN)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_url == "http://10.204.0.1:8444"
    assert GATEWAY_TOKEN not in repr(settings)
    # The key lives with the gateway: its absence here is not a disabled feature.
    assert "database_encryption (db_key)" not in settings.disabled_features()


def test_bot_settings_refuse_plain_http_to_a_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_GATEWAY_TOKEN", GATEWAY_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "http://45.138.157.65:8444")
    with pytest.raises(ValidationError, match="loopback or private"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_bot_settings_default_to_the_local_file(settings: Settings) -> None:
    assert settings.database_url is None
    assert settings.db_gateway_token is None
    database = main.build_database(settings)
    assert type(database) is Database


def test_bot_builds_the_remote_database_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "http://10.204.0.1:8444")
    monkeypatch.setenv("DB_GATEWAY_TOKEN", GATEWAY_TOKEN)
    database = main.build_database(Settings(_env_file=None))  # type: ignore[call-arg]
    assert isinstance(database, RemoteDatabase)
    assert database.base_url == "http://10.204.0.1:8444"


async def test_bot_startup_gives_up_on_an_unreachable_gateway(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"http://127.0.0.1:{unused_port()}")
    monkeypatch.setenv("DB_GATEWAY_TOKEN", GATEWAY_TOKEN)
    monkeypatch.setattr(main, "DATABASE_CONNECT_ATTEMPTS", 2)
    monkeypatch.setattr(main, "DATABASE_CONNECT_DELAY", 0.0)
    monkeypatch.setattr("db.remote.RETRY_DELAYS", (0.0, 0.0))
    with pytest.raises(SystemExit) as caught:
        await main.open_database(Settings(_env_file=None))  # type: ignore[call-arg]
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "unreachable" in err
    assert GATEWAY_TOKEN not in err
    assert "Traceback" not in err


async def test_bot_startup_connects_through_the_gateway(
    gateway: TestServer, local: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", str(gateway.make_url("/")))
    monkeypatch.setenv("DB_GATEWAY_TOKEN", GATEWAY_TOKEN)
    database = await main.open_database(Settings(_env_file=None))  # type: ignore[call-arg]
    try:
        assert isinstance(database, RemoteDatabase)
        await database.create_user(5, None, None)
        assert await local.get_user(5) is not None
    finally:
        await database.close()


async def test_bot_startup_refuses_a_wrong_token(
    gateway: TestServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wrong = "wrong-token-" + "0" * 30
    monkeypatch.setenv("DATABASE_URL", str(gateway.make_url("/")))
    monkeypatch.setenv("DB_GATEWAY_TOKEN", wrong)
    with pytest.raises(SystemExit) as caught:
        await main.open_database(Settings(_env_file=None))  # type: ignore[call-arg]
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "gateway error" in err
    assert wrong not in err


def gateway_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name in (
        "DB_GATEWAY_TOKEN",
        "DB_GATEWAY_TOKEN_NEXT",
        "DB_GATEWAY_HOST",
        "DB_GATEWAY_PORT",
        "DB_GATEWAY_CREATE",
        "DB_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_gateway_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(
        monkeypatch,
        DB_GATEWAY_TOKEN=f" {GATEWAY_TOKEN}\n",
        DB_GATEWAY_HOST="10.204.0.1",
    )
    settings = GatewaySettings()
    assert settings.db_gateway_host == "10.204.0.1"
    assert settings.db_gateway_port == 8444
    assert settings.db_gateway_token.get_secret_value() == GATEWAY_TOKEN
    assert settings.db_key is None
    assert settings.activity_logging_enabled is False
    assert GATEWAY_TOKEN not in repr(settings)


@pytest.mark.parametrize(
    "values",
    [
        {"DB_GATEWAY_HOST": "10.204.0.1"},
        {"DB_GATEWAY_TOKEN": "short", "DB_GATEWAY_HOST": "10.204.0.1"},
        {"DB_GATEWAY_TOKEN": GATEWAY_TOKEN},
        {"DB_GATEWAY_TOKEN": GATEWAY_TOKEN, "DB_GATEWAY_HOST": "0.0.0.0"},  # noqa: S104
        {"DB_GATEWAY_TOKEN": GATEWAY_TOKEN, "DB_GATEWAY_HOST": "45.138.157.65"},
        {"DB_GATEWAY_TOKEN": GATEWAY_TOKEN, "DB_GATEWAY_HOST": "localhost"},
        {
            "DB_GATEWAY_TOKEN": GATEWAY_TOKEN,
            "DB_GATEWAY_HOST": "10.204.0.1",
            "DB_GATEWAY_PORT": "80",
        },
    ],
)
def test_gateway_refuses_unsafe_settings(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str]
) -> None:
    gateway_env(monkeypatch, **values)
    with pytest.raises(ValidationError):
        GatewaySettings()


def test_gateway_main_names_the_field_not_the_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import db.gateway as gateway_module

    gateway_env(
        monkeypatch,
        **{"DB_GATEWAY_TOKEN": "short-secret", "DB_GATEWAY_HOST": "8.8.8.8"},
    )
    with pytest.raises(SystemExit) as caught:
        gateway_module.main()
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "db_gateway_host" in err
    assert "db_gateway_token" in err
    assert "short-secret" not in err
    assert "8.8.8.8" not in err


def test_gateway_main_reports_a_busy_address_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    import socket

    import db.gateway as gateway_module

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        gateway_env(
            monkeypatch,
            **{
                "DB_GATEWAY_TOKEN": GATEWAY_TOKEN,
                "DB_GATEWAY_HOST": "127.0.0.1",
                "DB_GATEWAY_PORT": str(taken.getsockname()[1]),
            },
        )
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.db"))
        monkeypatch.setenv("DB_GATEWAY_CREATE", "true")
        monkeypatch.setattr("logging_setup.setup_logging", lambda *_: None)
        with pytest.raises(SystemExit) as caught:
            gateway_module.main()
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Gateway cannot start (OSError)")
    assert "Traceback" not in err
    assert GATEWAY_TOKEN not in err


def test_gateway_does_not_create_a_database_by_accident(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    import db.gateway as gateway_module

    missing = tmp_path / "wrong" / "bot.db"
    gateway_env(
        monkeypatch,
        **{"DB_GATEWAY_TOKEN": GATEWAY_TOKEN, "DB_GATEWAY_HOST": "127.0.0.1"},
    )
    monkeypatch.setenv("DB_GATEWAY_PORT", str(unused_port()))
    monkeypatch.setenv("DATABASE_PATH", str(missing))
    monkeypatch.delenv("DB_GATEWAY_CREATE", raising=False)
    monkeypatch.setattr("logging_setup.setup_logging", lambda *_: None)
    with pytest.raises(SystemExit) as caught:
        gateway_module.main()
    assert caught.value.code == 2
    assert "does not exist or is empty" in capsys.readouterr().err
    assert not missing.parent.exists()
    # An empty file is the same mistake (a failed copy), not a database.
    empty = tmp_path / "bot.db"
    empty.touch()
    monkeypatch.setenv("DATABASE_PATH", str(empty))
    with pytest.raises(SystemExit) as caught:
        gateway_module.main()
    assert caught.value.code == 2
    assert empty.stat().st_size == 0


async def test_gateway_creates_a_database_only_when_told_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import db.gateway as gateway_module

    path = tmp_path / "bot.db"
    port = unused_port()
    gateway_env(
        monkeypatch,
        **{"DB_GATEWAY_TOKEN": GATEWAY_TOKEN, "DB_GATEWAY_HOST": "127.0.0.1"},
    )
    monkeypatch.setenv("DB_GATEWAY_PORT", str(port))
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DB_GATEWAY_CREATE", "true")
    # Signal handlers belong to the real process; here the task is cancelled.
    monkeypatch.setattr(
        asyncio.get_running_loop(), "add_signal_handler", lambda *_: None
    )
    serving = asyncio.create_task(gateway_module.serve(GatewaySettings()))
    client = RemoteDatabase(f"http://127.0.0.1:{port}", GATEWAY_TOKEN)
    try:
        for _ in range(50):
            try:
                await client.connect()
                break
            except DatabaseUnavailableError:
                client._down_until = 0.0
                await asyncio.sleep(0.05)
        assert await client.create_user(9, None, None) is True
    finally:
        await client.close()
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
    assert path.stat().st_size > 0


# ------------------------------------------------------- replacing the token
NEXT_TOKEN = "next-gateway-token-" + "1" * 24
NEXT_AUTH = {"Authorization": f"Bearer {NEXT_TOKEN}"}


@pytest.fixture
async def rotating(local: Database) -> AsyncIterator[TestServer]:
    """The gateway while its token is being replaced: both are accepted."""
    server = TestServer(create_gateway_app(local, GATEWAY_TOKEN, NEXT_TOKEN))
    await server.start_server()
    yield server
    await server.close()


async def token_facts(
    http: aiohttp.ClientSession, server: TestServer, headers: dict[str, str]
) -> dict[str, Any]:
    async with http.get(server.make_url("/v1/info"), headers=headers) as response:
        assert response.status == 200
        facts: dict[str, Any] = await response.json()
        return facts


async def test_both_tokens_work_during_a_rotation_and_nothing_else(
    rotating: TestServer, http: aiohttp.ClientSession
) -> None:
    for headers in (AUTH, NEXT_AUTH):
        status, _, _ = await post(http, rotating, call_body("ping"), headers)
        assert status == 200
    for headers in ({}, {"Authorization": "Bearer " + "x" * 40}):
        status, _, _ = await post(http, rotating, call_body("ping"), headers)
        assert status == 401
        async with http.get(rotating.make_url("/v1/info"), headers=headers) as response:
            assert response.status == 401


async def test_info_says_how_the_tokens_are_used(
    rotating: TestServer, http: aiohttp.ClientSession
) -> None:
    first = await token_facts(http, rotating, NEXT_AUTH)
    assert first["tokens_accepted"] == 2
    assert first["uptime_seconds"] >= 0
    assert first["token_use"] == {
        "current": {"calls": 0, "idle_seconds": None},
        "next": {"calls": 0, "idle_seconds": None},
    }
    # Asking is not using: neither question moved anything.
    assert (await token_facts(http, rotating, AUTH))["token_use"] == first["token_use"]
    status, _, _ = await post(http, rotating, call_body("ping"), NEXT_AUTH)
    assert status == 200
    after = (await token_facts(http, rotating, AUTH))["token_use"]
    assert after["next"]["calls"] == 1
    assert 0 <= after["next"]["idle_seconds"] <= 2
    assert after["current"] == {"calls": 0, "idle_seconds": None}


async def test_with_one_token_the_info_reports_one(
    gateway: TestServer, http: aiohttp.ClientSession
) -> None:
    facts = await token_facts(http, gateway, AUTH)
    assert facts["tokens_accepted"] == 1
    assert list(facts["token_use"]) == ["current"]


async def test_the_client_learns_how_many_tokens_are_accepted(
    rotating: TestServer, remote: RemoteDatabase
) -> None:
    assert remote.tokens_accepted == 1
    client = RemoteDatabase(str(rotating.make_url("/")), NEXT_TOKEN)
    await client.connect()
    try:
        assert client.tokens_accepted == 2
    finally:
        await client.close()


async def test_a_second_token_must_be_long_and_different(local: Database) -> None:
    with pytest.raises(ValueError, match="32"):
        create_gateway_app(local, GATEWAY_TOKEN, "short")
    with pytest.raises(ValueError, match="differ"):
        create_gateway_app(local, GATEWAY_TOKEN, GATEWAY_TOKEN)


def test_gateway_settings_take_a_second_token(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(
        monkeypatch,
        DB_GATEWAY_TOKEN=GATEWAY_TOKEN,
        DB_GATEWAY_HOST="10.204.0.1",
        DB_GATEWAY_TOKEN_NEXT=f" {NEXT_TOKEN}\n",
    )
    settings = GatewaySettings()
    assert settings.db_gateway_token_next is not None
    assert settings.db_gateway_token_next.get_secret_value() == NEXT_TOKEN
    assert NEXT_TOKEN not in repr(settings)


@pytest.mark.parametrize("blank", ["", "  ", "\n"])
def test_a_blank_second_token_is_no_second_token(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    gateway_env(
        monkeypatch,
        DB_GATEWAY_TOKEN=GATEWAY_TOKEN,
        DB_GATEWAY_HOST="10.204.0.1",
        DB_GATEWAY_TOKEN_NEXT=blank,
    )
    assert GatewaySettings().db_gateway_token_next is None


@pytest.mark.parametrize("second", ["short-secret", GATEWAY_TOKEN])
def test_a_bad_second_token_refuses_to_start_and_names_only_the_field(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    second: str,
) -> None:
    import db.gateway as gateway_module

    gateway_env(
        monkeypatch,
        DB_GATEWAY_TOKEN=GATEWAY_TOKEN,
        DB_GATEWAY_HOST="10.204.0.1",
        DB_GATEWAY_TOKEN_NEXT=second,
    )
    with pytest.raises(SystemExit) as caught:
        gateway_module.main()
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "db_gateway_token_next" in err
    assert second not in err
    assert GATEWAY_TOKEN not in err


def test_short_tokens_are_refused_everywhere(local: Database) -> None:
    with pytest.raises(ValueError, match="32"):
        create_gateway_app(local, "short")
    with pytest.raises(ValueError, match="32"):
        RemoteDatabase("http://10.204.0.1:8444", "short")
