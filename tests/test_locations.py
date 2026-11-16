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
from services.hiddify import HiddifyClient, HiddifyError
from tests.conftest import make_init_data
from webhook_server import create_webhook_app

SUB_BASE = "https://sub.cyberdefency.com:8443"


def _subscription_mode(services: Any, base: str | None = SUB_BASE) -> None:
    services.settings = services.settings.model_copy(
        update={"config_delivery": "subscription", "subscription_base_url": base}
    )
    services.provisioning.settings = services.settings


def test_subscription_url_is_made_absolute_and_validated() -> None:
    extract = HiddifyClient.extract_subscription_url
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
    services: Any, hiddify: Any
) -> None:
    _subscription_mode(services)
    await services.db.create_user(720001, "user", None)
    result = await services.provisioning.accept_offer_and_start_trial(720001)
    node = (await services.db.get_user(720001))["node_username"]
    assert result.config_link == f"{SUB_BASE}/sub/{node}"
    assert await services.provisioning.fresh_config(720001) == result.config_link
    # Rotating the UUID issues a new subscription URL.
    new = await services.provisioning.revoke_link(720001)
    fresh = (await services.db.get_user(720001))["node_username"]
    assert fresh != node
    assert new.startswith(f"{SUB_BASE}/sub/{fresh}")
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


async def test_health_lists_core_and_enabled_nodes(hiddify: Any) -> None:
    hiddify.nodes = [
        {"name": "🇫🇮 Финляндия", "status": "connected"},
        {"name": "🇳🇱 Нидерланды", "status": "error"},
        {"name": "Резерв", "status": "disabled"},
    ]
    health = ServiceHealth(hiddify, Admin(), 1, "🇩🇪 Германия")
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


async def test_alert_after_two_failures_and_recovery_once(hiddify: Any) -> None:
    admin = Admin()
    health = ServiceHealth(hiddify, admin, 1, "Основной")
    hiddify.core_started = False
    await health.poll()
    assert admin.messages == []  # one failed check is not an outage yet
    await health.poll()
    await health.poll()
    assert len(admin.messages) == 1
    assert "🔴 Сервер Основной" in admin.messages[0]
    hiddify.core_started = True
    await health.poll()
    await health.poll()
    assert len(admin.messages) == 2
    assert "🟢 Сервер Основной снова работает" in admin.messages[1]


async def test_unreachable_panel_is_reported_separately(hiddify: Any) -> None:
    admin = Admin()
    health = ServiceHealth(hiddify, admin, 1, "Основной")
    hiddify.core_error = HiddifyError("down")
    report = await health.poll()
    report = await health.poll()
    assert not report.panel_ok
    assert "Панель Hiddify" in admin.messages[0]
    assert "не удалось проверить" in servers_text(report, subscription=False)


async def test_node_names_are_escaped_in_alerts(hiddify: Any) -> None:
    admin = Admin()
    hiddify.nodes = [{"name": "<b>x</b>", "status": "error"}]
    health = ServiceHealth(hiddify, admin, 1, "Основной", failures_to_alert=1)
    await health.poll()
    assert "&lt;b&gt;x&lt;/b&gt;" in admin.messages[0]


async def test_profile_shows_known_outage(services: Any, hiddify: Any) -> None:
    from handlers.profile import outage_suffix

    services.health = ServiceHealth(hiddify, Admin(), 1, "Основной")
    assert outage_suffix(services) == ""
    services.health.last = HealthReport(0, True, (Location("Основной", False),))
    assert OUTAGE_NOTICE in outage_suffix(services)


async def test_servers_api(services: Any, hiddify: Any) -> None:
    services.health = ServiceHealth(hiddify, Admin(), 1, "🇩🇪 Германия")
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


async def test_forbidden_node_list_is_not_reported_as_an_outage(hiddify: Any) -> None:
    from services.hiddify import HiddifyForbidden

    admin = Admin()
    hiddify.nodes_error = HiddifyForbidden("get_nodes")
    health = ServiceHealth(hiddify, admin, 1, "Основной", failures_to_alert=1)
    report = await health.poll()
    assert report.panel_ok
    assert report.locations == (Location("Основной", True),)
    assert admin.messages == []

    hiddify.core_error = HiddifyForbidden("get_core")
    report = await health.poll()
    assert report.panel_ok
    assert report.locations == ()
    assert "список серверов сейчас недоступен" in servers_text(
        report, subscription=False
    )
    assert admin.messages == []


async def test_node_list_failure_after_core_ok_is_an_outage(hiddify: Any) -> None:
    admin = Admin()
    hiddify.nodes_error = HiddifyError("timeout")
    health = ServiceHealth(hiddify, admin, 1, "Основной", failures_to_alert=1)
    report = await health.poll()
    assert not report.panel_ok
    assert "Панель Hiddify" in admin.messages[0]


async def test_hiddify_403_raises_forbidden() -> None:
    import httpx

    from services.hiddify import HiddifyForbidden

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "You're not allowed"})

    client = HiddifyClient(
        "https://hiddify.test",
        "11111111-1111-4111-8111-111111111111",
        "secretadmin",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HiddifyForbidden):
        await client.get_core()
    with pytest.raises(HiddifyForbidden):
        await client.get_users_usage()
    await client.close()


async def test_hiddify_page_that_is_not_json_is_a_panel_error() -> None:
    """A proxy error page must not escape as a crash past the payment retry."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Bad gateway</html>")

    client = HiddifyClient(
        "https://hiddify.test",
        "11111111-1111-4111-8111-111111111111",
        "secretadmin",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HiddifyError, match="not JSON"):
        await client.get_core()
    with pytest.raises(HiddifyError, match="not JSON"):
        await client.get_users_usage()
    await client.close()
