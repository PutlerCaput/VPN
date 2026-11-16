"""Fixes from the November audit: answers during a database outage, the banned
check cache, log fields that make failures readable, limiter pruning, per-user
locks, the API's 503 and the bounded subscription download."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, ErrorEvent, Message, Update, User
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.middleware import context_middleware
from db.remote import DatabaseUnavailableError
from logging_setup import JsonFormatter
from middlewares.banned import BannedMiddleware
from middlewares.errors import (
    FAILED_TEXT,
    PAYMENT_DELAYED_TEXT,
    UNAVAILABLE_TEXT,
    on_error,
)
from services.hiddify import SUBSCRIPTION_MAX_BYTES, HiddifyClient, HiddifyError
from services.scheduler import Jobs
from services.security import SlidingWindowLimiter

USER = User(id=100501, is_bot=False, first_name="Test")
NODE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _message(text: str) -> Message:
    message = Message.model_construct(
        message_id=1,
        date=datetime.fromtimestamp(0, UTC),
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        text=text,
    )
    object.__setattr__(message, "answer", AsyncMock())
    return message


def _unavailable() -> DatabaseUnavailableError:
    try:
        try:
            raise TimeoutError
        except TimeoutError as exc:
            raise DatabaseUnavailableError("database gateway is unavailable") from exc
    except DatabaseUnavailableError as exc:
        return exc


# ------------------------------------------------------------ error handler


async def test_a_message_gets_an_answer_when_the_database_is_away() -> None:
    message = _message("👤 Профиль")
    update = Update.model_construct(update_id=1, message=message)
    handled = await on_error(ErrorEvent(update=update, exception=_unavailable()))
    assert handled is True
    message.answer.assert_awaited_once_with(UNAVAILABLE_TEXT)  # type: ignore[attr-defined]


async def test_a_button_gets_an_alert_and_other_failures_a_general_answer() -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    update = Update.model_construct(update_id=2, callback_query=callback)
    await on_error(ErrorEvent(update=update, exception=RuntimeError("boom")))
    callback.answer.assert_awaited_once_with(FAILED_TEXT, show_alert=True)


async def test_an_already_answered_button_gets_the_answer_in_the_chat() -> None:
    message = _message("menu")
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.answer = AsyncMock(
        side_effect=TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="1"),
            message="query is too old",
        )
    )
    update = Update.model_construct(update_id=3, callback_query=callback)
    assert await on_error(ErrorEvent(update=update, exception=_unavailable())) is True
    message.answer.assert_awaited_once_with(UNAVAILABLE_TEXT)  # type: ignore[attr-defined]


async def test_a_failing_answer_does_not_raise_a_second_error() -> None:
    message = _message("x")
    message.answer.side_effect = OSError("network")  # type: ignore[attr-defined]
    update = Update.model_construct(update_id=4, message=message)
    assert await on_error(ErrorEvent(update=update, exception=_unavailable())) is True


async def test_a_payment_that_was_not_recorded_is_not_asked_to_be_repeated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = _message("")
    object.__setattr__(message, "successful_payment", MagicMock())
    update = Update.model_construct(update_id=5, message=message)
    with caplog.at_level(logging.ERROR, logger="middlewares.errors"):
        assert await on_error(ErrorEvent(update=update, exception=_unavailable()))
    message.answer.assert_awaited_once_with(PAYMENT_DELAYED_TEXT)  # type: ignore[attr-defined]
    assert [r.msg for r in caplog.records] == ["bot_payment_handler_failed"]


def test_the_dispatcher_has_the_error_handler() -> None:
    # build_dispatcher attaches the one root router, which can be attached once
    # per process, so the registration and its call site are checked apart.
    from pathlib import Path

    from aiogram import Dispatcher

    from middlewares import register_error_handler

    dp = Dispatcher()
    register_error_handler(dp)
    assert any(h.callback is on_error for h in dp.errors.handlers)
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    assert "register_error_handler(dp)" in main_py.read_text(encoding="utf-8")


# ------------------------------------------------------- banned status cache


class CountingDb:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    async def get_user(self, telegram_id: int) -> dict[str, Any]:
        self.calls += 1
        return {"telegram_id": telegram_id, "status": self.status}


async def test_menu_taps_reuse_a_recent_answer_and_start_always_reads_fresh() -> None:
    db = CountingDb("active")
    middleware = BannedMiddleware(db)
    handler = AsyncMock(return_value="ok")
    for _ in range(3):
        assert (
            await middleware(handler, _message("👤 Профиль"), {"event_from_user": USER})
            == "ok"
        )
    assert db.calls == 1
    data: dict[str, Any] = {"event_from_user": USER}
    assert await middleware(handler, _message("/start"), data) == "ok"
    assert db.calls == 2
    # /start and /paysupport read the record, so it is always a fresh one.
    assert data["db_user"]["status"] == "active"


async def test_a_ban_reaches_the_menus_after_the_cache_time() -> None:
    db = CountingDb("active")
    middleware = BannedMiddleware(db, cache_seconds=0.05)
    handler = AsyncMock(return_value="ok")
    assert (
        await middleware(handler, _message("menu"), {"event_from_user": USER}) == "ok"
    )
    db.status = "banned"
    await asyncio.sleep(0.06)
    message = _message("menu")
    assert await middleware(handler, message, {"event_from_user": USER}) is None
    message.answer.assert_awaited_once()  # type: ignore[attr-defined]


async def test_the_status_cache_is_bounded() -> None:
    middleware = BannedMiddleware(CountingDb("active"), cache_size=3)
    handler = AsyncMock(return_value="ok")
    for user_id in range(10):
        user = User(id=user_id + 1, is_bot=False, first_name="U")
        await middleware(handler, _message("menu"), {"event_from_user": user})
    assert len(middleware._status) == 3


# ------------------------------------------------------------------ logging


def test_job_names_library_templates_and_causes_are_logged_without_data() -> None:
    formatter = JsonFormatter()
    try:
        raise _unavailable()
    except DatabaseUnavailableError:
        record = logging.LogRecord(
            "services.scheduler",
            logging.ERROR,
            __file__,
            1,
            "job_failed",
            (),
            sys.exc_info(),
        )
    record.job = "anomaly_poller"
    payload = json.loads(formatter.format(record))
    assert payload["job"] == "anomaly_poller"
    assert payload["error_type"] == "DatabaseUnavailableError"
    assert payload["cause_type"] == "TimeoutError"

    library = logging.LogRecord(
        "apscheduler.scheduler",
        logging.WARNING,
        __file__,
        1,
        'Execution of job "%s" skipped: maximum number of running instances '
        "reached (%d)",
        ("Jobs.payment_reconciliation-secret-arg", 1),
        None,
    )
    rendered = formatter.format(library)
    assert json.loads(rendered)["template"].startswith('Execution of job "%s" skipped')
    assert "secret-arg" not in rendered


def test_preformatted_text_and_odd_job_values_stay_out_of_the_log() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "aiogram.event",
        logging.ERROR,
        __file__,
        1,
        "Request to https://api.telegram.org/bot123:ABC failed",
        (),
        None,
    )
    record.job = "Has Spaces/and-a-token:123"
    rendered = formatter.format(record)
    assert "api.telegram.org" not in rendered
    assert "template" not in json.loads(rendered)
    assert "job" not in json.loads(rendered)
    # A template with a newline-separated tail keeps only its first line.
    record = logging.LogRecord(
        "aiogram.event", logging.ERROR, __file__, 1, "Cause %s\n%s: %s", (1, 2, 3), None
    )
    assert json.loads(formatter.format(record))["template"] == "Cause %s"


# ------------------------------------------------------- housekeeping prune


async def test_housekeeping_prunes_the_limiters_even_when_the_database_is_away() -> (
    None
):
    limiter = SlidingWindowLimiter(1, window_seconds=0.01)
    limiter.hit("203.0.113.9")
    await asyncio.sleep(0.02)
    jobs = Jobs.__new__(Jobs)
    jobs.limiters = [limiter]
    jobs.db = MagicMock()
    jobs.db.purge_refresh_tokens = AsyncMock(side_effect=_unavailable())
    with pytest.raises(DatabaseUnavailableError):
        await jobs.housekeeping()
    assert len(limiter) == 0


# ----------------------------------------------------------- per-user locks


async def test_per_user_locks_do_not_pile_up() -> None:
    from services.provisioning import ProvisioningService

    provisioning = ProvisioningService.__new__(ProvisioningService)
    import weakref

    provisioning._locks = weakref.WeakValueDictionary()
    first = provisioning._lock(1)
    assert provisioning._lock(1) is first
    async with first:
        assert provisioning._lock(1) is first
    for user_id in range(2, 200):
        async with provisioning._lock(user_id):
            pass
    del first
    gc.collect()
    assert len(provisioning._locks) == 0


# ----------------------------------------------------------------- API 503


async def test_the_api_answers_503_with_retry_after_when_the_database_is_away() -> None:
    async def handler(_request: web.Request) -> web.Response:
        raise _unavailable()

    app = web.Application(middlewares=[context_middleware])
    app.router.add_get("/api/v1/sub/x/", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/v1/sub/x/")
        assert response.status == 503
        assert response.headers["Retry-After"] == "30"
        assert (await response.json()) == {"error": "temporarily_unavailable"}
    finally:
        await client.close()


# ------------------------------------------------- bounded subscription read


def _hiddify(body: bytes) -> HiddifyClient:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})

    return HiddifyClient(
        "https://panel.example.com",
        "k" * 32,
        "secretadmin",
        user_proxy_path="secretuser",
        subscription_base_url="https://sub.example.com",
        transport=httpx.MockTransport(respond),
    )


async def test_the_subscription_download_stops_at_the_limit() -> None:
    client = _hiddify(b"x" * (SUBSCRIPTION_MAX_BYTES + 1))
    try:
        with pytest.raises(HiddifyError, match="too large"):
            await client.fetch_subscription(NODE, "v2rayNG/1.9")
    finally:
        await client.close()
    client = _hiddify("vless://ok#Привет".encode())
    try:
        status, body, headers = await client.fetch_subscription(NODE, "v2rayNG/1.9")
        assert (status, body) == (200, "vless://ok#Привет")
        assert headers["content-type"] == "text/plain"
    finally:
        await client.close()
