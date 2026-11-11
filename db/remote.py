"""RemoteDatabase: the ``Database`` API, served by ``db.gateway`` on another host.

A drop-in for ``Database``: every public coroutine method is forwarded to the
gateway under the same name with the same arguments, so the SQL keeps running
next to the database file and the callers do not change.

What differs from a local database:

* A call can fail because the other server is unreachable. That raises
  ``DatabaseUnavailableError`` (a ``ConnectionError``, deliberately not a
  ``RuntimeError`` or ``ValueError``, which the data layer uses for its own
  conditions). After a failure the client fails fast for a few seconds
  instead of queueing callers behind timeouts.
* A lost response is retried with the same request id; the gateway answers a
  repeated id from memory instead of running the method twice.
* ``transaction()`` and ``conn`` do not exist here: a multi-statement change
  belongs in a ``Database`` method, which then runs remotely as one call.
* Timestamps written by ``Database`` come from the gateway's clock;
  ``clock_skew_seconds`` is measured at ``connect()`` for the doctor.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, NoReturn

import aiohttp
import aiosqlite

from db.database import Database, PendingPaymentLimitError
from db.endpoint import MIN_TOKEN_LENGTH, PROTOCOL_VERSION, validate_gateway_url
from db.gateway import remote_methods

# Pauses before the second and third attempt of one call.
RETRY_DELAYS = (0.2, 0.6)
# Some timestamps come from the caller and some from the gateway's clock;
# beyond this difference the two servers need their time fixed.
MAX_CLOCK_SKEW_SECONDS = 30
DOMAIN_ERRORS: dict[str, type[Exception]] = {
    "PendingPaymentLimitError": PendingPaymentLimitError,
    "IntegrityError": sqlite3.IntegrityError,
    "ValueError": ValueError,
    "RuntimeError": RuntimeError,
}


class DatabaseUnavailableError(ConnectionError):
    """The gateway could not be reached; the call was retried and gave no answer."""


class RemoteDatabaseError(Exception):
    """The gateway answered, but refused the call or failed inside it."""


class RemoteDatabase(Database):
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        connect_timeout: float = 3.0,
        retries: int = 2,
        cooldown: float = 3.0,
    ) -> None:
        # Database.__init__ is not called on purpose: there is no local file,
        # key or connection on this side.
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"token must be at least {MIN_TOKEN_LENGTH} characters")
        self.base_url = validate_gateway_url(base_url)
        self.path = self.base_url
        self.activity_logging_enabled = False
        self.clock_skew_seconds = 0
        self._remote_encrypted = False
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout, connect=connect_timeout)
        self._retries = retries
        self._cooldown = cooldown
        self._down_until = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._conn = None

    @property
    def encrypted(self) -> bool:
        return self._remote_encrypted

    @property
    def conn(self) -> NoReturn:
        raise RuntimeError("RemoteDatabase has no local connection")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        raise RuntimeError(
            "RemoteDatabase has no local transaction: put the statements into "
            "a Database method, it then runs remotely as one call"
        )
        yield  # pragma: no cover

    async def connect(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers=self._headers,
                connector=aiohttp.TCPConnector(limit=16),
                trust_env=False,
            )
        facts = await self.info()
        if facts.get("protocol") != PROTOCOL_VERSION:
            await self.close()
            raise RemoteDatabaseError(
                "the database gateway speaks another protocol version: "
                "update both servers to the same release"
            )
        self._remote_encrypted = bool(facts.get("encrypted"))
        self.activity_logging_enabled = bool(facts.get("activity_logging_enabled"))
        self.clock_skew_seconds = int(facts.get("server_time") or 0) - int(time.time())

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def info(self) -> dict[str, Any]:
        """Backend facts from the gateway (see ``db.gateway`` ``/v1/info``)."""
        status, payload = await self._exchange("GET", "/v1/info", None)
        facts = _decode(payload)
        if status != 200 or not isinstance(facts, dict):
            raise RemoteDatabaseError(f"info: HTTP {status}")
        return facts

    async def _exchange(
        self, verb: str, path: str, body: bytes | None
    ) -> tuple[int, bytes]:
        session = self._session
        if session is None:
            raise RuntimeError("Database is not connected")
        if time.monotonic() < self._down_until:
            raise DatabaseUnavailableError("database gateway is unavailable")
        last: BaseException | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(
                    RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                )
            try:
                async with session.request(
                    verb, self.base_url + path, data=body
                ) as response:
                    payload = await response.read()
                    status = response.status
            except (aiohttp.ClientError, TimeoutError) as exc:
                last = exc
                continue
            if status in {502, 503, 504}:
                last = RemoteDatabaseError(f"HTTP {status}")
                continue
            return status, payload
        self._down_until = time.monotonic() + self._cooldown
        raise DatabaseUnavailableError("database gateway is unavailable") from last

    async def _call(self, method: str, args: dict[str, Any]) -> Any:
        for name, value in args.items():
            if isinstance(value, bool):
                # SQLite stores a bool as 0/1; keep that when it crosses the wire.
                args[name] = int(value)
            elif value is not None and not isinstance(value, int | str):
                raise TypeError(
                    f"{method}({name}=...) must be int, str or None, "
                    f"not {type(value).__name__}"
                )
        # One id for every attempt of this call: a retry is answered from the
        # gateway's memory instead of running the method again.
        body = json.dumps(
            {"id": uuid.uuid4().hex, "method": method, "args": args},
            separators=(",", ":"),
        ).encode()
        status, payload = await self._exchange("POST", "/v1/call", body)
        answer = _decode(payload)
        if not isinstance(answer, dict):
            raise RemoteDatabaseError(f"{method}: HTTP {status}, body is not JSON")
        error = answer.get("error")
        if status == 200 and error is None and "result" in answer:
            return answer["result"]
        kind = str(error.get("type")) if isinstance(error, dict) else "unknown"
        if status == 200 and kind in DOMAIN_ERRORS and isinstance(error, dict):
            raise DOMAIN_ERRORS[kind](str(error.get("message") or ""))
        raise RemoteDatabaseError(f"{method}: HTTP {status} {kind}")


def _decode(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except ValueError:
        return None


def _forward(name: str, signature: inspect.Signature) -> Callable[..., Awaitable[Any]]:
    rows_are_tuples = "list[tuple[" in str(signature.return_annotation)

    async def method(self: RemoteDatabase, *args: Any, **kwargs: Any) -> Any:
        # Binding raises the same TypeError a local call would.
        bound = signature.bind(self, *args, **kwargs)
        arguments = dict(bound.arguments)
        del arguments["self"]
        result = await self._call(name, arguments)
        if rows_are_tuples and isinstance(result, list):
            return [tuple(row) for row in result]
        return result

    method.__name__ = name
    method.__qualname__ = f"RemoteDatabase.{name}"
    method.__doc__ = f"``Database.{name}`` on the gateway."
    return method


# Every public coroutine method, including ones added later: nothing can be
# left running local SQL by mistake (tests/test_db_gateway.py checks this).
for _name, _signature in remote_methods().items():
    setattr(RemoteDatabase, _name, _forward(_name, _signature))
