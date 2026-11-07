"""All-locations subscription delivery, server list and outage alerts."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from config import Settings
from services.health import (
    OUTAGE_NOTICE,
    HealthReport,
    Location,
    ServiceHealth,
    servers_text,
)
from services.marzban import MarzbanClient, MarzbanError
from tests.conftest import make_init_data
from webhook_server import create_webhook_app

SUB_BASE = "https://sub.cyberdefency.com:8443"


def _subscription_mode(services: Any, base: str | None = SUB_BASE) -> None:
    services.settings = services.settings.model_copy(
        update={"config_delivery": "subscription", "subscription_base_url": base}
    )
    services.provisioning.settings = services.settings


def test_subscription_url_is_made_absolute_and_validated() -> None:
    extract = MarzbanClient.extract_subscription_url
    assert extract({"subscription_url": "/sub/abc"}, SUB_BASE) == f"{SUB_BASE}/sub/abc"
    assert extract({"subscription_url": "/sub/abc"}, None) is None
    assert (
        extract({"subscription_url": "https://s.example.org/sub/x"}, None)
        == "https://s.example.org/sub/x"
    )
    assert extract({"subscription_url": "http://s.example.org/sub/x"}, None) is None
    assert extract({"subscription_url": "https://u:p@s.example.org/x"}, None) is None
    assert extract({}, SUB_BASE) is None


def test_subscription_base_url_setting_is_an_https_origin(settings: Settings) -> None:
    base = settings.model_dump()
    ok = Settings(**{**base, "subscription_base_url": f"{SUB_BASE}/"})
    assert ok.subscription_base_url == SUB_BASE
    for bad in ("http://sub.example.org", f"{SUB_BASE}/sub", "https://"):
        with pytest.raises(ValueError, match="subscription_base_url"):
            Settings(**{**base, "subscription_base_url": bad})


async def test_trial_and_config_use_the_subscription_link(
    services: Any, marzban: Any
) -> None:
    _subscription_mode(services)
    await services.db.create_user(720001, "user", None)
    result = await services.provisioning.accept_offer_and_start_trial(720001)
    node = (await services.db.get_user(720001))["node_username"]
    assert result.config_link == f"{SUB_BASE}/sub/{node}"
    assert await services.provisioning.fresh_config(720001) == result.config_link
    # Resetting the link changes the subscription token too.
    new = await services.provisioning.revoke_link(720001)
    assert new.startswith(f"{SUB_BASE}/sub/{node}")
    assert new != result.config_link


async def test_without_a_usable_subscription_url_the_single_link_is_kept(
    services: Any,
) -> None:
    _subscription_mode(services, base=None)
    await services.db.create_user(720002, "user", None)
    result = await services.provisioning.accept_offer_and_start_trial(720002)
    assert result.config_link is not None
    assert result.config_link.startswith("vless://")


async def test_default_delivery_stays_a_single_link(services: Any) -> None:
    await services.db.create_user(720003, "user", None)
    result = await services.provisioning.accept_offer_and_start_trial(720003)
    assert result.config_link is not None
    assert result.config_link.startswith("vless://")


class Admin:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def telegram(self, telegram_id: int, text: str) -> bool:
        self.messages.append(text)
        return True


async def test_health_lists_core_and_enabled_nodes(marzban: Any) -> None:
    marzban.nodes = [
        {"name": "🇫🇮 Финляндия", "status": "connected"},
        {"name": "🇳🇱 Нидерланды", "status": "error"},
        {"name": "Резерв", "status": "disabled"},
    ]
    health = ServiceHealth(marzban, Admin(), 1, "🇩🇪 Германия")
    report = await health.check()
    assert report.locations == (
        Location("🇩🇪 Германия", True),
        Location("🇫🇮 Финляндия", True),
        Location("🇳🇱 Нидерланды", False),
    )
    assert not report.all_online
    text = servers_text(report, subscription=True)
    assert "🟢 🇩🇪 Германия — работает" in text
    assert "🔴 🇳🇱 Нидерланды — недоступен" in text
    assert "Резерв" not in text


async def test_alert_after_two_failures_and_recovery_once(marzban: Any) -> None:
    admin = Admin()
    health = ServiceHealth(marzban, admin, 1, "Основной")
    marzban.core_started = False
    await health.poll()
    assert admin.messages == []  # one failed check is not an outage yet
    await health.poll()
    await health.poll()
    assert len(admin.messages) == 1
    assert "🔴 Сервер Основной" in admin.messages[0]
    marzban.core_started = True
    await health.poll()
    await health.poll()
    assert len(admin.messages) == 2
    assert "🟢 Сервер Основной снова работает" in admin.messages[1]


async def test_unreachable_panel_is_reported_separately(marzban: Any) -> None:
    admin = Admin()
    health = ServiceHealth(marzban, admin, 1, "Основной")
    marzban.core_error = MarzbanError("down")
    report = await health.poll()
    report = await health.poll()
    assert not report.panel_ok
    assert "Панель Marzban" in admin.messages[0]
    assert "не удалось проверить" in servers_text(report, subscription=False)


async def test_node_names_are_escaped_in_alerts(marzban: Any) -> None:
    admin = Admin()
    marzban.nodes = [{"name": "<b>x</b>", "status": "error"}]
    health = ServiceHealth(marzban, admin, 1, "Основной", failures_to_alert=1)
    await health.poll()
    assert "&lt;b&gt;x&lt;/b&gt;" in admin.messages[0]


async def test_profile_shows_known_outage(services: Any, marzban: Any) -> None:
    from handlers.profile import outage_suffix

    services.health = ServiceHealth(marzban, Admin(), 1, "Основной")
    assert outage_suffix(services) == ""
    services.health.last = HealthReport(0, True, (Location("Основной", False),))
    assert OUTAGE_NOTICE in outage_suffix(services)


async def test_servers_api(services: Any, marzban: Any) -> None:
    services.health = ServiceHealth(marzban, Admin(), 1, "🇩🇪 Германия")
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    token = services.settings.bot_token.get_secret_value()
    async with TestClient(TestServer(app)) as client:
        auth = await client.post(
            "/api/v1/auth/telegram", json={"initData": make_init_data(token, 720010)}
        )
        headers = {"Authorization": f"Bearer {(await auth.json())['access_token']}"}
        body = await (await client.get("/api/v1/servers", headers=headers)).json()
        assert body["panel_ok"] is True
        assert body["locations"] == [{"name": "🇩🇪 Германия", "online": True}]
        assert (await client.get("/api/v1/servers")).status == 401


async def test_kill_switch_guide_is_offered(services: Any) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message

    from handlers.onboarding import setup_action
    from keyboards.onboarding import setup_kb
    from services.connection_guide import KILL_SWITCH_TEXT

    callbacks = [b.callback_data for row in setup_kb().inline_keyboard for b in row]
    assert "setup:killswitch" in callbacks
    message = MagicMock()
    message.__class__ = Message
    message.chat.type = "private"
    message.chat.id = 5
    message.answer = AsyncMock()
    callback = MagicMock()
    callback.message = message
    callback.from_user.id = 5
    callback.data = "setup:killswitch"
    callback.answer = AsyncMock()
    await setup_action(callback, AsyncMock(), services)
    assert message.answer.await_args.args[0] == KILL_SWITCH_TEXT
    assert "Блокировать подключения без VPN" in KILL_SWITCH_TEXT


async def test_forbidden_node_list_is_not_reported_as_an_outage(marzban: Any) -> None:
    from services.marzban import MarzbanForbidden

    admin = Admin()
    marzban.nodes_error = MarzbanForbidden("get_nodes")
    health = ServiceHealth(marzban, admin, 1, "Основной", failures_to_alert=1)
    report = await health.poll()
    assert report.panel_ok
    assert report.locations == (Location("Основной", True),)
    assert admin.messages == []

    marzban.core_error = MarzbanForbidden("get_core")
    report = await health.poll()
    assert report.panel_ok
    assert report.locations == ()
    assert "список серверов сейчас недоступен" in servers_text(
        report, subscription=False
    )
    assert admin.messages == []


async def test_node_list_failure_after_core_ok_is_an_outage(marzban: Any) -> None:
    admin = Admin()
    marzban.nodes_error = MarzbanError("timeout")
    health = ServiceHealth(marzban, admin, 1, "Основной", failures_to_alert=1)
    report = await health.poll()
    assert not report.panel_ok
    assert "Панель Marzban" in admin.messages[0]


async def test_marzban_403_raises_forbidden() -> None:
    import httpx

    from services.marzban import MarzbanForbidden

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(403, json={"detail": "You're not allowed"})

    client = MarzbanClient(
        "http://marzban.test", "a", "b", "t", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(MarzbanForbidden):
        await client.get_nodes()
    with pytest.raises(MarzbanForbidden):
        await client.get_core()
    await client.close()
