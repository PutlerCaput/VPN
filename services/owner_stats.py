"""The owner's /stats: how the service is doing, in counts and sums only.

No names, no Telegram ids, no addresses: the numbers are enough to run a small
service and say nothing about any customer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.server_traffic import TrafficOutlook, fmt_share


def rubles(kopecks: int) -> str:
    whole, cents = divmod(int(kopecks), 100)
    return f"{whole:,}".replace(",", " ") + f",{cents:02d} ₽"


def amount_text(currency: str, total: int) -> str:
    if currency == "RUB":
        return rubles(total)
    if currency == "XTR":
        return f"{total} ⭐"
    return f"{total / 100:.2f} {currency}".replace(".", ",")


def money_line(rows: list[dict[str, Any]], period: str) -> str:
    """The sums of one period, one currency after another."""
    parts = [
        f"{amount_text(str(row['currency']), int(row['total']))} ({row['n']})"
        for row in sorted(rows, key=lambda r: str(r["currency"]))
        if row["period"] == period
    ]
    return ", ".join(parts) or "—"


def percent(part: int, whole: int) -> str:
    return f"{part * 100 // whole}%" if whole else "—"


def format_stats(
    stats: dict[str, Any], now_ts: int, outlook: TrafficOutlook | None = None
) -> str:
    users = stats["users"]
    total = int(users.get("total", 0))
    trials = int(users.get("trials", 0))
    paying = int(stats["paying_users"])
    when = datetime.fromtimestamp(now_ts, UTC).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📊 <b>Сводка</b>, {when} UTC",
        f"Клиенты: всего {total}; активны {users.get('active', 0)}; "
        f"истекли {users.get('expired', 0)}; приостановлены "
        f"{users.get('suspended', 0)}; заблокированы {users.get('banned', 0)}",
        f"Новых за 7 дней: {users.get('new_7d', 0)}",
        f"Пробный → платный: {paying} из {trials} ({percent(paying, trials)})",
        f"Подписка заканчивается: за 3 дня {users.get('expiring_3d', 0)}, "
        f"за 7 дней {users.get('expiring_7d', 0)}; не продлили за 7 дней: "
        f"{users.get('lapsed_7d', 0)}",
        f"Оплаты за 30 дней: {money_line(stats['money'], 'now')}",
        f"Предыдущие 30 дней: {money_line(stats['money'], 'before')}",
        f"Возвраты за 30 дней: {stats['refunds_30d']}",
    ]
    if outlook is not None and outlook.limit_bytes > 0:
        lines.append(
            f"Трафик сервера: {fmt_share(outlook.used_share)} лимита, прогноз на "
            f"период {fmt_share(outlook.expected_share)} (/traffic)"
        )
    return "\n".join(lines)
