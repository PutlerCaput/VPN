"""AUTOBAN needs enough IDS confidence; a single weak signal only alerts."""

from __future__ import annotations

from typing import Any

from services.scheduler import AUTOBAN_CONFIDENCE, confidence
from tests.conftest import FakeHiddify, FakeNotifier


async def _active(services: Any, telegram_id: int) -> str:
    await services.db.create_user(telegram_id, "user", None)
    await services.provisioning.accept_offer_and_start_trial(telegram_id)
    user = await services.db.get_user(telegram_id)
    return str(user["node_username"])


def test_confidence_weights() -> None:
    assert confidence({"PORT_SCANNING"}) < AUTOBAN_CONFIDENCE
    assert confidence({"DDOS_ATTACK"}) < AUTOBAN_CONFIDENCE
    assert confidence({"PORT_SCANNING", "MALICIOUS_PROBE"}) >= AUTOBAN_CONFIDENCE
    assert confidence({"CYBERCRIME"}) >= AUTOBAN_CONFIDENCE
    assert confidence({"UNKNOWN_KIND"}) == 0
    assert confidence({"CYBERCRIME", "DDOS_ATTACK"}) == 100


async def test_single_signal_alerts_admin_without_ban(
    services: Any, notifier: FakeNotifier, hiddify: FakeHiddify
) -> None:
    node = await _active(services, 610001)
    banned = await services.monitor.handle_report(node, {"PORT_SCANNING"}, {})
    assert banned is False
    assert (await services.db.get_user(610001))["status"] == "active"
    assert hiddify.users[node]["status"] == "active"
    admin = services.settings.admin_telegram_id
    assert any(
        tid == admin and "Уверенность 60/90" in text for tid, text in notifier.messages
    )
    kinds = [a["kind"] for a in await services.db.list_anomalies(610001)]
    assert kinds == []  # Sub-threshold signals are retained only in memory.


async def test_signals_accumulate_into_ban(services: Any, hiddify: FakeHiddify) -> None:
    node = await _active(services, 610002)
    assert not await services.monitor.handle_report(node, {"PORT_SCANNING"}, {})
    assert await services.monitor.handle_report(node, {"MALICIOUS_PROBE"}, {})
    assert (await services.db.get_user(610002))["status"] == "banned"
    assert hiddify.users[node]["status"] == "disabled"
    # Already banned: further reports are ignored.
    assert not await services.monitor.handle_report(node, {"CYBERCRIME"}, {})


async def test_unknown_kinds_and_nodes_are_ignored(services: Any) -> None:
    node = await _active(services, 610003)
    assert not await services.monitor.handle_report(node, {"BLOCKED_SITE"}, {})
    assert not await services.monitor.handle_report("u999999", {"CYBERCRIME"}, {})
    assert await services.db.list_anomalies(610003) == []


async def test_weak_note_flag_does_not_ban(services: Any, hiddify: FakeHiddify) -> None:
    node = await _active(services, 610004)
    hiddify.panel_usage = [
        {
            "username": node,
            "status": "active",
            "used_traffic": 10,
            "note": "ids: DDOS_ATTACK",
        },
    ]
    await services.monitor.poll()
    assert (await services.db.get_user(610004))["status"] == "active"
