"""Admin-only commands. Every handler checks from_user.id == ADMIN_TELEGRAM_ID."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message

from services.audit import audit
from services.container import Services
from services.owner_stats import format_stats
from services.scheduler import SEVERE_KINDS
from services.security import html_escape, is_valid_payment_id
from services.server_traffic import report as traffic_report
from services.telegram_rub import PROVIDER_PREFIX as RUB_PROVIDER_PREFIX

router = Router(name="admin")


class IsAdmin(Filter):
    async def __call__(self, message: Message, services: Services) -> bool:
        return (
            message.from_user is not None
            and message.from_user.id == services.settings.admin_telegram_id
            and message.chat.type == "private"
        )


class CanManage(Filter):
    async def __call__(self, message: Message, services: Services) -> bool:
        if services.settings.admin_telegram_read_only:
            await message.answer(
                "🔒 Telegram работает только для просмотра состояния: /status. "
                "Операции управления отключены."
            )
            return False
        return True


router.message.filter(IsAdmin())


@router.message(Command("status"))
async def cmd_status(message: Message, services: Services) -> None:
    try:
        db_ok = await services.db.ping()
    except Exception:
        db_ok = False
    mode = (
        "только просмотр"
        if services.settings.admin_telegram_read_only
        else "управление"
    )
    await message.answer(
        f"Бот: отвечает\nБД: {'доступна' if db_ok else 'ошибка проверки'}\n"
        f"Режим Telegram: {mode}\n"
        f"Время проверки: {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC\n"
        "VPN-туннель: этой командой не проверяется."
    )


@router.message(Command("traffic"))
async def cmd_traffic(message: Message, services: Services) -> None:
    if services.traffic is None:
        await message.answer(
            "Учёт трафика сервера выключен (SERVER_TRAFFIC_LIMIT_GB=0)."
        )
        return
    await message.answer(traffic_report(await services.traffic.compute()))


@router.message(Command("stats"))
async def cmd_stats(message: Message, services: Services) -> None:
    now_ts = int(time.time())
    outlook = services.traffic.outlook if services.traffic is not None else None
    await message.answer(
        format_stats(await services.db.owner_stats(now_ts), now_ts, outlook)
    )


@router.message(Command("confirm"), CanManage())
async def cmd_confirm(
    message: Message, command: CommandObject, services: Services
) -> None:
    payment_id = (command.args or "").strip()
    if not is_valid_payment_id(payment_id):
        await message.answer("Использование: <code>/confirm &lt;payment_id&gt;</code>")
        return
    payment = await services.db.get_payment(payment_id)
    if payment is None:
        await message.answer("Платёж не найден.")
        return
    if payment["method"] != "sbp":
        await message.answer("Вручную подтверждаются только платежи СБП.")
        return
    await audit(
        services.db,
        message.from_user.id if message.from_user else None,
        "admin_confirm",
        payment_id,
    )
    if await services.provisioning.process_paid_payment(payment_id):
        await message.answer(
            f"✅ Платёж <code>{payment_id}</code> подтверждён: "
            f"{payment['amount'] // 100} ₽. "
            "Выдайте чек сегодня («Мой налог» или онлайн-касса).",
        )
    else:
        await message.answer("Платёж уже был обработан ранее.")


REFUND_RESULTS = {
    "refunded": "↩️ Звёзды возвращены, оплаченный период снят с подписки.",
    "not_found": "Платёж Stars не найден.",
    "not_refundable": "Возвращаются только оплаченные и не возвращённые платежи Stars.",
    "telegram_error": "⚠️ Telegram не принял возврат. Ничего не изменено.",
}


@router.message(Command("refund"), CanManage())
async def cmd_refund(
    message: Message, command: CommandObject, services: Services
) -> None:
    payment_id = (command.args or "").strip()
    if not is_valid_payment_id(payment_id):
        await message.answer("Использование: <code>/refund &lt;payment_id&gt;</code>")
        return
    payment = await services.db.get_payment(payment_id)
    provider_id = str((payment or {}).get("provider_id") or "")
    if payment is not None and provider_id.startswith(
        (RUB_PROVIDER_PREFIX, "yookassa:")
    ):
        # The Bot API cannot refund YooKassa payments: the money is returned
        # in the YooKassa dashboard; here only the paid period is taken back.
        # (The bot also notices such a refund by itself: services/refund_watch.py.)
        if payment["status"] != "paid":
            await message.answer(REFUND_RESULTS["not_refundable"])
            return
        await audit(
            services.db,
            message.from_user.id if message.from_user else None,
            "admin_refund",
            payment_id,
        )
        await services.provisioning.revoke_refunded_payment(payment_id)
        yookassa_id = html_escape(provider_id.split(":", 1)[1])
        await message.answer(
            "↩️ Оплаченный период снят с подписки. Верните деньги в личном "
            f"кабинете ЮKassa: платёж <code>{yookassa_id}</code> → «Вернуть»."
        )
        return
    if services.stars is None:
        await message.answer("Оплата Telegram Stars не включена.")
        return
    await audit(
        services.db,
        message.from_user.id if message.from_user else None,
        "admin_refund",
        payment_id,
    )
    result = await services.stars.refund(payment_id)
    await message.answer(REFUND_RESULTS.get(result, result))


@router.message(Command("cancel_unpaid"), CanManage())
async def cmd_cancel_unpaid(message: Message, services: Services) -> None:
    """Cancel every unpaid order. A payment that still arrives is credited."""
    count = await services.db.cancel_all_unpaid()
    await audit(
        services.db,
        message.from_user.id if message.from_user else None,
        "admin_cancel_unpaid",
        str(count),
    )
    await message.answer(
        f"🧹 Отменено неоплаченных счетов: <b>{count}</b>. Если по какому-то из "
        "них всё же придёт оплата, бот её зачислит."
    )


@router.message(Command("unsuspend"), CanManage())
async def cmd_unsuspend(
    message: Message, command: CommandObject, services: Services
) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(
            "Использование: <code>/unsuspend &lt;telegram_id&gt;</code>"
        )
        return
    ok = await services.provisioning.unsuspend(int(arg))
    await message.answer("✅ Готово." if ok else "Пользователь не приостановлен.")


@router.message(Command("ban"), CanManage())
async def cmd_ban(message: Message, command: CommandObject, services: Services) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in SEVERE_KINDS:
        kinds = ", ".join(sorted(SEVERE_KINDS))
        await message.answer(
            "Использование: <code>/ban &lt;telegram_id&gt; &lt;KIND&gt;</code>\n"
            f"KIND: {kinds}"
        )
        return
    ok = await services.provisioning.autoban(
        int(parts[0]), None, "admin", {"flags": [parts[1]]}
    )
    if ok:
        await message.answer("🚫 Заблокирован.")
    elif await services.db.get_pending_ban(int(parts[0])):
        await message.answer(
            "⏳ Блокировка поставлена в очередь: Hiddify пока не подтвердил "
            "отключение узла. Бот повторит попытку автоматически."
        )
    else:
        await message.answer("Пользователь не найден или уже заблокирован.")


def order_sum(order: dict[str, Any]) -> str:
    """An order's sum in the unit it was paid in."""
    amount = int(order["amount"])
    if order["currency"] == "RUB":
        return f"{amount / 100:.2f} ₽".replace(".", ",")
    if order["currency"] == "XTR":
        return f"{amount} ⭐"
    return f"{amount / 100:.2f} {order['currency']}".replace(".", ",")


@router.message(Command("user"), CanManage())
async def cmd_user(
    message: Message, command: CommandObject, services: Services
) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: <code>/user &lt;telegram_id&gt;</code>")
        return
    user = await services.db.get_user(int(arg))
    if user is None:
        await message.answer("Не найден.")
        return
    paid = await services.db.count_paid_payments(int(arg))
    anomalies = await services.db.list_anomalies(int(arg))
    orders = [
        f"<code>{o['payment_id']}</code> {order_sum(o)}, {o['months']} мес., "
        f"{o['status']}"
        for o in await services.db.payment_history(int(arg), 5)
    ]
    await message.answer(
        f"<code>{user['telegram_id']}</code> @{user['username'] or '—'}\n"
        f"status={user['status']} expires_at={user['expires_at']}\n"
        f"node=<code>{user['node_username'] or '—'}</code> "
        f"referred_by={user['referred_by']}\n"
        f"paid_payments={paid} anomalies={len(anomalies)}"
        + (
            "\nПоследние заказы (номер для /refund):\n" + "\n".join(orders)
            if orders
            else ""
        ),
    )


@router.message(F.reply_to_message, CanManage())
async def admin_support_reply(message: Message, services: Services) -> None:
    replied = message.reply_to_message
    if replied is None:
        return
    target = await services.db.get_support_target(replied.message_id)
    if target is None:
        await message.answer("Это не копия обращения в поддержку.")
        return
    try:
        await services.bot.send_message(target, "💬 <b>Ответ поддержки:</b>")
        await message.copy_to(target)
        await message.answer("✅ Отправлено.")
    except TelegramAPIError:
        await message.answer("⚠️ Пользователь недоступен (возможно, заблокировал бота).")
