from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from config import Settings
from db.database import Database
from handlers.payments import cmd_usdt
from keyboards.main_menu import channel_kb
from logging_setup import JsonFormatter
from services.scheduler import AnomalyMonitor


def test_conservative_defaults_and_hidden_usdt() -> None:
    settings = Settings()
    assert settings.autoban_mode == "review"
    assert not settings.activity_logging_enabled
    for args in ((), (12, "prime")):
        keyboard = channel_kb(*args)
        assert all(
            "usdt" not in (b.callback_data or "").lower()
            and "usdt" not in b.text.lower()
            for row in keyboard.inline_keyboard
            for b in row
        )


async def test_usdt_remains_available_through_separate_command(services: Any) -> None:
    await services.db.create_user(987650, "crypto_customer", None)
    await services.db.accept_offer(987650)
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=987650)
    await cmd_usdt(message, services)
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    assert any(
        b.callback_data == "tier:usdt:standard"
        for row in keyboard.inline_keyboard
        for b in row
    )
    services.cryptopay = None
    message.reset_mock()
    await cmd_usdt(message, services)
    assert "недоступна" in message.answer.call_args.args[0]


async def test_minimal_mode_preserves_accounts_but_not_activity(db: Database) -> None:
    await db.create_user(987654, "ordinary", None)
    await db.add_conn_log(987654, "u987654", 1, 2, 3)
    await db.add_anomaly(987654, "u987654", "IDS_SIGNAL", "signal")
    await db.add_audit(987654, "offer_accepted", None, None)
    await db.upsert_traffic_day(
        "u987654",
        "2026-11-02",
        baseline_bytes=1,
        last_bytes=100,
        last_online_at="private-session-time",
    )
    for table in ("conn_logs", "anomalies", "audit_log", "traffic_daily"):
        assert await db._fetchone(f"SELECT count(*) AS n FROM {table}") == {"n": 0}  # noqa: S608
    assert await db.get_user(987654) is not None
    traffic = await db.get_traffic_day("u987654", "2026-11-02")
    assert traffic is not None
    assert traffic["last_bytes"] == 100
    assert traffic["last_online_at"] is None


async def test_review_mode_does_not_disable_high_confidence_user(services: Any) -> None:
    services.settings.autoban_mode = "review"
    await services.db.create_user(987655, "ordinary", None)
    await services.provisioning.accept_offer_and_start_trial(987655)
    node = (await services.db.get_user(987655))["node_username"]
    assert not await services.monitor.handle_report(node, {"CYBERCRIME"}, {})
    assert (await services.db.get_user(987655))["status"] == "active"
    assert services.hiddify.users[node]["status"] == "active"
    assert await services.db.list_anomalies(987655) == []


async def test_unconfirmed_ban_does_not_create_completed_evidence(
    services: Any,
) -> None:
    from services.hiddify import HiddifyError

    await services.db.create_user(987656, "ordinary", None)
    await services.provisioning.accept_offer_and_start_trial(987656)
    node = (await services.db.get_user(987656))["node_username"]
    services.hiddify.fail_disable_once = HiddifyError("unavailable")
    assert not await services.provisioning.autoban(987656, node, "flags", {})
    assert await services.db.list_anomalies(987656) == []
    assert await services.db.get_pending_ban(987656) is not None
    assert await services.provisioning.autoban(987656, node, "flags", {})
    rows = await services.db.list_anomalies(987656)
    assert [r["kind"] for r in rows] == ["AUTOBAN"]


def test_operational_logs_omit_identifiers_payloads_and_exception_messages() -> None:
    record = logging.LogRecord(
        "services.test", logging.ERROR, __file__, 1, "operation_failed", (), None
    )
    record.telegram_id = 987654
    record.token = "secret-token-value"
    record.ip_hash = "user-address-hash"
    record.detail = {"url": "https://private.example/secret"}
    try:
        raise ValueError("sensitive-exception-payload")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    rendered = JsonFormatter().format(record)
    assert json.loads(rendered)["error_type"] == "ValueError"
    for value in (
        "987654",
        "secret-token-value",
        "user-address-hash",
        "private.example",
        "sensitive-exception-payload",
    ):
        assert value not in rendered


async def test_signal_memory_expires_without_persisting(
    services: Any, monkeypatch: Any
) -> None:
    import time

    import services.scheduler as scheduler

    clock = [100000.0]
    # The scheduler's view of the clock only: the event loop runs on
    # time.monotonic too, and freezing that stalls every timer and timeout.
    monkeypatch.setattr(
        scheduler,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    await services.db.create_user(987657, "ordinary", None)
    await services.provisioning.accept_offer_and_start_trial(987657)
    await services.monitor.handle_report("u987657", {"PORT_SCANNING"}, {})
    clock[0] += scheduler.SIGNAL_WINDOW_SECONDS + 1
    assert not await services.monitor.handle_report("u987657", {"MALICIOUS_PROBE"}, {})
    assert await services.db.list_anomalies(987657) == []
    fresh = AnomalyMonitor(
        services.db, services.hiddify, services.provisioning, services.settings
    )
    assert await fresh._recent_signals(987657) == set()
