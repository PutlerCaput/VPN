"""Admin-only commands. Every handler checks from_user.id == ADMIN_TELEGRAM_ID."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message

from services.audit import audit
from services.container import Services
from services.scheduler import SEVERE_KINDS
from services.security import is_valid_payment_id

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
            "⏳ Блокировка поставлена в очередь: Marzban пока не подтвердил "
            "отключение узла. Бот повторит попытку автоматически."
        )
    else:
        await message.answer("Пользователь не найден или уже заблокирован.")


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
    await message.answer(
        f"<code>{user['telegram_id']}</code> @{user['username'] or '—'}\n"
        f"status={user['status']} expires_at={user['expires_at']}\n"
        f"node=<code>{user['node_username'] or '—'}</code> "
        f"referred_by={user['referred_by']}\n"
        f"paid_payments={paid} anomalies={len(anomalies)}",
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
