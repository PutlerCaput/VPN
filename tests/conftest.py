"""Shared fixtures: in-memory DB, fake Hiddify, fake notifier, settings."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")
os.environ.setdefault("BOT_USERNAME", "test_node_bot")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("PUBLIC_BASE_URL", "https://app.test")
os.environ.setdefault("HIDDIFY_API_KEY", "11111111-1111-4111-8111-111111111111")
os.environ.setdefault("JWT_SECRET", "a" * 64)
os.environ.setdefault("CRYPTO_PAY_TOKEN", "12345:TESTCRYPTOPAYTOKEN")
os.environ.setdefault("SECRETS_DIR", "/nonexistent")

from aiohttp.test_utils import TestServer

from config import Settings, device_note
from db.database import Database
from db.gateway import create_gateway_app
from db.remote import RemoteDatabase
from services.hiddify import HiddifyNotFound, traffic_policy

GATEWAY_TOKEN = "test-gateway-token-" + "0" * 24
REMOTE_BACKEND = os.environ.get("TEST_DB_BACKEND") == "remote"


class LoopbackRemoteDatabase(RemoteDatabase):
    """What ``TEST_DB_BACKEND=remote`` hands to the suite.

    Public methods cross the gateway, as in production. The private helpers
    tests use to arrange and inspect rows go straight to the database behind it.
    """

    def __init__(self, base_url: str, token: str, local: Database) -> None:
        super().__init__(base_url, token)
        self._local = local

    def __getattr__(self, name: str) -> Any:
        if name == "_local":
            raise AttributeError(name)
        return getattr(self._local, name)

    @property
    def conn(self) -> Any:
        return self._local.conn

    def transaction(self) -> Any:
        return self._local.transaction()

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return await self._local._execute(sql, params)

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        return await self._local._fetchone(sql, params)

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        return await self._local._fetchall(sql, params)

    @property
    def activity_logging_enabled(self) -> bool:
        return self._local.activity_logging_enabled

    @activity_logging_enabled.setter
    def activity_logging_enabled(self, value: bool) -> None:
        # The switch lives next to the data; the constructor's default is ignored.
        local = self.__dict__.get("_local")
        if local is not None:
            local.activity_logging_enabled = value


@asynccontextmanager
async def through_gateway(database: Database) -> AsyncIterator[LoopbackRemoteDatabase]:
    """A connected client of a gateway serving ``database`` on loopback."""
    server = TestServer(create_gateway_app(database, GATEWAY_TOKEN))
    await server.start_server()
    remote = LoopbackRemoteDatabase(str(server.make_url("/")), GATEWAY_TOKEN, database)
    try:
        await remote.connect()
        yield remote
    finally:
        await remote.close()
        await server.close()


class FakeHiddify:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.panel_usage: list[dict[str, Any]] = []
        self.fail_modify_once: dict[str, Exception] = {}
        self.fail_disable_once: Exception | None = None
        self.core_started = True
        self.core_error: Exception | None = None
        self.nodes: list[dict[str, Any]] = []
        self.nodes_error: Exception | None = None

    def _payload(self, username: str) -> dict[str, Any]:
        user = self.users[username]
        revision = user.get("revision", 0)
        suffix = f"&rev={revision}" if revision else ""
        return {
            **user,
            "links": [
                f"vless://{username}@node.example.com:443?security=reality{suffix}"
            ],
            "subscription_url": f"/sub/{username}{suffix.replace('&', '-')}",
        }

    async def create_user(
        self,
        username: str,
        days: int | None = None,
        *,
        expire: int | None = None,
        tier: str = "basic",
        data_limit: int | None = None,
        data_limit_reset_strategy: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("create", username))
        limit, strategy = traffic_policy(tier, data_limit, data_limit_reset_strategy)
        self.users[username] = {
            "uuid": username,
            "username": username,
            "expire": expire or int(time.time()) + (days or 1) * 86400,
            "status": "active",
            "note": device_note(tier),
            "data_limit": limit,
            "data_limit_reset_strategy": strategy,
            "used_traffic": 0,
        }
        return self._payload(username)

    async def revoke_subscription(self, username: str) -> dict[str, Any]:
        if username not in self.users:
            raise HiddifyNotFound(username)
        self.calls.append(("revoke", username))
        new_uuid = str(uuid.uuid4())
        record = self.users.pop(username)
        record["revision"] = record.get("revision", 0) + 1
        record["uuid"] = new_uuid
        record["username"] = new_uuid
        self.users[new_uuid] = record
        return self._payload(new_uuid)

    async def modify_user(
        self,
        username: str,
        *,
        expire: int | None = None,
        status: str | None = None,
        note: str | None = None,
        data_limit: int | None = None,
        data_limit_reset_strategy: str | None = None,
    ) -> dict[str, Any]:
        if username not in self.users:
            raise HiddifyNotFound(username)
        failure = self.fail_modify_once.pop(username, None)
        if failure is not None:
            raise failure
        self.calls.append(("modify", username))
        if expire is not None:
            self.users[username]["expire"] = expire
        if status is not None:
            self.users[username]["status"] = status
        if note is not None:
            self.users[username]["note"] = note
        if data_limit is not None:
            self.users[username]["data_limit"] = data_limit
        if data_limit_reset_strategy is not None:
            self.users[username]["data_limit_reset_strategy"] = (
                data_limit_reset_strategy
            )
        return self._payload(username)

    async def get_user(self, username: str) -> dict[str, Any]:
        if username not in self.users:
            raise HiddifyNotFound(username)
        return self._payload(username)

    async def disable_user(self, username: str) -> dict[str, Any]:
        self.calls.append(("disable", username))
        if self.fail_disable_once is not None:
            failure = self.fail_disable_once
            self.fail_disable_once = None
            raise failure
        return await self.modify_user(username, status="disabled")

    async def enable_user(self, username: str) -> dict[str, Any]:
        return await self.modify_user(username, status="active")

    async def get_vless_link(self, username: str) -> str:
        return str(self._payload(username)["links"][0])

    async def get_users_usage(self) -> list[dict[str, Any]]:
        return self.panel_usage

    async def get_core(self) -> dict[str, Any]:
        if self.core_error is not None:
            raise self.core_error
        return {"version": "1.8.24", "started": self.core_started}

    async def get_nodes(self) -> list[dict[str, Any]]:
        if self.nodes_error is not None:
            raise self.nodes_error
        return self.nodes

    async def close(self) -> None:
        return None


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.pushes: list[tuple[int, str]] = []

    async def telegram(
        self, telegram_id: int, text: str, *, animation: bool = False
    ) -> bool:
        self.messages.append((telegram_id, text))
        return True

    async def notify(
        self,
        telegram_id: int,
        text: str,
        *,
        push_title: str | None = None,
        push_body: str | None = None,
        url: str = "/",
        animation: bool = False,
    ) -> bool:
        self.messages.append((telegram_id, text))
        if push_title:
            self.pushes.append((telegram_id, push_title))
        return True


@pytest.fixture
def settings() -> Settings:
    return Settings(autoban_mode="automatic")


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    # TEST_DB_KEY=<64 hex> runs the whole suite against SQLCipher;
    # TEST_DB_BACKEND=remote runs it through the database gateway.
    database = Database(":memory:", os.environ.get("TEST_DB_KEY") or None)
    await database.connect()
    try:
        if REMOTE_BACKEND:
            async with through_gateway(database) as remote:
                yield remote
        else:
            yield database
    finally:
        await database.close()


@pytest.fixture
def hiddify() -> FakeHiddify:
    return FakeHiddify()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def provisioning(
    db: Database, hiddify: FakeHiddify, notifier: FakeNotifier, settings: Settings
) -> Any:
    from services.provisioning import ProvisioningService

    return ProvisioningService(db, hiddify, notifier, settings)  # type: ignore[arg-type]


@pytest.fixture
def services(
    db: Database,
    hiddify: FakeHiddify,
    notifier: FakeNotifier,
    settings: Settings,
    provisioning: Any,
) -> Any:
    from unittest.mock import MagicMock

    from services.billing import BillingService
    from services.container import Services
    from services.cryptopay import CryptoPayClient
    from services.push import PushService
    from services.sbp import SbpService
    from services.scheduler import AnomalyMonitor

    sbp = SbpService(
        "manual", "https://qr.nspk.ru/test", "https://qr.nspk.ru/test", None
    )
    cryptopay = CryptoPayClient(settings.crypto_pay_token.get_secret_value())  # type: ignore[union-attr]
    return Services(
        settings=settings,
        db=db,
        bot=MagicMock(),
        hiddify=hiddify,  # type: ignore[arg-type]
        cryptopay=cryptopay,
        sbp=sbp,
        push=PushService(db, None, None, "mailto:test@example.com"),
        notifier=notifier,  # type: ignore[arg-type]
        provisioning=provisioning,
        billing=BillingService(db, sbp, cryptopay, allow_legacy_sbp=True),
        monitor=AnomalyMonitor(db, hiddify, provisioning, settings),  # type: ignore[arg-type]
    )


def make_init_data(
    bot_token: str,
    telegram_id: int = 424242,
    auth_date: int | None = None,
    start_param: str | None = None,
) -> str:
    import hashlib
    import hmac
    import json
    from urllib.parse import urlencode

    params = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAH" + str(telegram_id),
        "user": json.dumps(
            {
                "id": telegram_id,
                "first_name": "Ivan",
                "username": "ivan_test",
                "language_code": "ru",
            },
            separators=(",", ":"),
        ),
    }
    if start_param is not None:
        params["start_param"] = start_param
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)
