"""The privacy policy is published wherever personal data is collected."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from handlers.legal import cb_privacy, cmd_privacy, privacy_text
from handlers.start import OFFER_PROMPT
from keyboards.main_menu import accept_offer_kb
from webhook_server import create_webhook_app

ROOT = Path(__file__).resolve().parent.parent


def test_policy_names_the_bot_and_has_no_placeholders() -> None:
    text = privacy_text()
    assert "152-ФЗ" in text
    assert "@Cyberdefencybot" in text
    assert "<" not in text  # no "<имя>"-style placeholders left
    for section in ("Цели", "Сроки хранения", "Права Пользователя", "Меры защиты"):
        assert section in text


def test_policy_is_linked_from_the_offer_the_welcome_and_the_site() -> None:
    assert "/privacy" in (ROOT / "legal" / "oferta.md").read_text(encoding="utf-8")
    assert "политику обработки персональных данных" in OFFER_PROMPT
    buttons = [b for row in accept_offer_kb().inline_keyboard for b in row]
    assert any(b.callback_data == "privacy:full" for b in buttons)
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "@public_privacy path /privacy" in caddy
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY legal/privacy.md /srv/legal/privacy.md" in docker


async def test_bot_sends_the_policy_by_command_and_button(services: Any) -> None:
    message = AsyncMock()
    await cmd_privacy(message, services)
    kwargs = message.answer_document.await_args.kwargs
    assert services.settings.privacy_url in kwargs["caption"]
    callback = AsyncMock()
    callback.message = None
    await cb_privacy(callback, services)
    callback.answer.assert_awaited_once()


async def test_api_serves_the_policy_without_login(services: Any) -> None:
    app = create_webhook_app(services.bot, None, services)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/v1/legal/privacy")
        assert response.status == 200
        assert (await response.json())["text"] == privacy_text()


async def test_retention_purges_old_support_links_incidents_and_audit(
    services: Any,
) -> None:
    db = services.db
    now = int(time.time())
    old, fresh = now - 4 * 365 * 86400, now - 86400
    for ts, msg_id in ((old, 1), (fresh, 2)):
        await db._execute(
            "INSERT INTO support_messages (admin_message_id, telegram_id, created_at) "
            "VALUES (?, ?, ?)",
            (msg_id, 5, ts),
        )
        await db._execute(
            "INSERT INTO anomalies (telegram_id, kind, created_at) VALUES (?, ?, ?)",
            (5, "scan", ts),
        )
        await db._execute(
            "INSERT INTO audit_log (telegram_id, action, created_at) VALUES (?, ?, ?)",
            (5, "test", ts),
        )
    assert await db.purge_retained_records(now) == 3
    for table in ("support_messages", "anomalies", "audit_log"):
        rows = await db._fetchall(f"SELECT created_at FROM {table}", ())  # noqa: S608
        assert [r["created_at"] for r in rows] == [fresh]
