"""Shared message builders for handlers."""

from __future__ import annotations

import time
from typing import Any

from aiogram.types import BufferedInputFile, Message

from config import TIERS
from keyboards.onboarding import setup_kb
from services.provisioning import format_date
from services.qr import render_qr_png
from services.security import html_escape

STATUS_LABELS = {
    "pending": "⚪️ Не активирован",
    "active": "🟢 Активен",
    "suspended": "🟠 Приостановлен",
    "expired": "🔴 Истёк",
    "banned": "⛔️ Заблокирован",
}

DISCLAIMER = (
    "18+. Сервис шифрует трафик на участке «устройство — узел» в публичных сетях. "
    "Он не предназначен для доступа к ресурсам, ограниченным законодательством РФ, "
    "и не скрывает личность пользователя."
)

APPS_HINT = (
    "Выберите устройство под сообщением — бот покажет, "
    "куда вставить ссылку и как включить подключение."
)


def days_left(expires_at: int | None) -> int:
    if not expires_at:
        return 0
    return max(0, int((int(expires_at) - time.time()) // 86400))


def status_text(user: dict[str, Any]) -> str:
    status = STATUS_LABELS.get(user["status"], user["status"])
    lines = [
        f"<b>Статус:</b> {status}",
        f"<b>Активен до:</b> {format_date(user['expires_at'])}",
    ]
    if user["status"] == "active":
        lines.append(f"<b>Осталось дней:</b> {days_left(user['expires_at'])}")
    tier = TIERS.get(str(user.get("tier") or ""))
    if tier and user["status"] in ("active", "suspended", "expired"):
        lines.append(f"<b>Тариф:</b> {tier.title} (до {tier.devices} устройств)")
    if user["node_username"]:
        lines.append(f"<b>Узел:</b> <code>{user['node_username']}</code>")
    return "\n".join(lines)


async def send_config(message: Message, link: str, expires_at: int | None) -> None:
    if message.chat.type != "private":
        return
    await message.answer(
        "🔐 <b>Ваша конфигурация подключения</b>\n\n"
        "Ссылка для импорта (нажмите, чтобы скопировать):\n"
        f"<code>{html_escape(link)}</code>\n\n"
        f"Активна до: <b>{format_date(expires_at)}</b>\n\n{APPS_HINT}\n\n"
        "⚠️ Не передавайте ссылку третьим лицам: она привязана к вашему аккаунту.",
        reply_markup=setup_kb(),
    )
    await message.answer_photo(
        BufferedInputFile(render_qr_png(link), filename="config.png"),
        caption="QR-код конфигурации для сканирования в клиенте.",
    )
