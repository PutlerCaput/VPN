"""Shared message builders for handlers."""

from __future__ import annotations

import time
from typing import Any

from aiogram.types import BufferedInputFile, Message

from config import TIERS, format_gb
from db.database import Database
from keyboards.main_menu import config_actions_kb
from keyboards.onboarding import setup_kb
from services.provisioning import TrafficStatus, format_date
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


def traffic_line(traffic: TrafficStatus) -> str:
    used = format_gb(traffic.used_bytes)
    if traffic.limit_bytes <= 0:
        return f"<b>Трафик:</b> {used} ГБ, без ограничения"
    line = f"<b>Трафик:</b> {used} из {format_gb(traffic.limit_bytes)} ГБ"
    if traffic.reset_strategy == "month":
        line += " (обнуляется каждые 30 дней)"
    if traffic.limited:
        line += "\n⛔️ Трафик израсходован: подключение приостановлено до обнуления"
    return line


async def is_trial(db: Database, user: dict[str, Any]) -> bool:
    """A user on the free trial who has never paid."""
    return bool(user.get("trial_used")) and (
        await db.count_paid_payments(int(user["telegram_id"])) == 0
    )


def status_text(
    user: dict[str, Any],
    traffic: TrafficStatus | None = None,
    *,
    trial: bool = False,
) -> str:
    status = STATUS_LABELS.get(user["status"], user["status"])
    lines = [
        f"<b>Статус:</b> {status}",
        f"<b>Активен до:</b> {format_date(user['expires_at'])}",
    ]
    if user["status"] == "active":
        lines.append(f"<b>Осталось дней:</b> {days_left(user['expires_at'])}")
    tier = TIERS.get(str(user.get("tier") or ""))
    if tier and trial and user["status"] == "active":
        lines.append(
            f"<b>Тариф:</b> пробный период ({tier.title}, до {tier.devices} "
            "устройств)"
        )
    elif tier and user["status"] in ("active", "suspended", "expired"):
        terms = f"до {tier.devices} устройств"
        if tier.traffic_gb:
            terms += f", {tier.traffic_gb} ГБ на 30 дней"
        lines.append(f"<b>Тариф:</b> {tier.title} ({terms})")
    if traffic is not None and user["status"] == "active":
        lines.append(traffic_line(traffic))
    if user["node_username"]:
        lines.append(f"<b>Узел:</b> <code>{user['node_username']}</code>")
    return "\n".join(lines)


async def send_config(
    message: Message,
    link: str,
    expires_at: int | None,
    *,
    awg: bool = False,
) -> None:
    if message.chat.type != "private":
        return
    kind = (
        "Ссылка-подписка: в ней все доступные серверы, клиент сам обновляет "
        "их список (нажмите, чтобы скопировать):\n"
        if link.startswith("https://")
        else "Ссылка для импорта (нажмите, чтобы скопировать):\n"
    )
    await message.answer(
        "🔐 <b>Ваша конфигурация подключения</b>\n\n"
        f"{kind}"
        f"<code>{html_escape(link)}</code>\n\n"
        f"Активна до: <b>{format_date(expires_at)}</b>\n\n{APPS_HINT}\n\n"
        "⚠️ Не передавайте ссылку третьим лицам: она привязана к вашему аккаунту.",
        reply_markup=setup_kb(awg),
    )
    await message.answer_photo(
        BufferedInputFile(render_qr_png(link), filename="config.png"),
        caption=(
            "QR-код конфигурации для сканирования в клиенте.\n\n"
            "Если ссылка попала к посторонним или вы сменили устройство, "
            "сбросьте её: старые копии перестанут работать."
        ),
        reply_markup=config_actions_kb(awg),
    )
