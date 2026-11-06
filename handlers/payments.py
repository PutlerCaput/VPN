"""Tariff selection and payment creation (SBP for Russia, USDT outside)."""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config import DEFAULT_TIER, TIER_PRICES_RUB, TIER_PRICES_USDT, TIERS
from keyboards.main_menu import (
    BTN_PAY,
    channel_kb,
    sbp_payment_kb,
    tariffs_kb,
    tiers_kb,
    usdt_payment_kb,
    yookassa_payment_kb,
)
from services.billing import BillingError
from services.container import Services
from services.cryptopay import is_valid_invoice_url
from services.qr import render_qr_png
from services.security import is_valid_payment_id
from services.yookassa import is_valid_email

logger = logging.getLogger(__name__)
router = Router(name="payments")

_USDT_MENTION = re.compile(
    r"(?<![a-z0-9])(?:usdt|crypto\s*pay|cryptobot|крипто(?:валют\w*|оплат\w*))(?![a-z0-9])",
    re.IGNORECASE,
)
_USDT_PAYMENT_INTENT = re.compile(
    r"\b(?:оплатить|оплата|платить|плат[её]ж|рассчит\w*|можно|как|хочу|"
    r"принима\w*|доступ\w*|способ|кнопк\w*|pay|payment|accept\w*|invoice)\b",
    re.IGNORECASE,
)

CHANNEL_TEXT = (
    "💳 <b>Оплата через ЮKassa</b>\n\n"
    "Оплатите в рублях через СБП или банковской картой на странице ЮKassa. "
    "Доступные способы определяются платёжным сервисом и вашим банком. "
    "Чек формируется в установленном порядке."
)


def tiers_text(title: str, enabled: list[str]) -> str:
    """Tier overview: only what the service really provides today."""
    blocks = [f"⭐ <b>Выберите тариф</b> ({title})"]
    for key, tier in TIERS.items():
        if key not in enabled:
            continue
        features = "\n".join(f"• {item}" for item in tier.features)
        price = (
            f"{TIER_PRICES_USDT[key][1]:.2f} USDT"
            if title == "USDT"
            else f"{TIER_PRICES_RUB[key][1]} ₽"
        )
        blocks.append(f"<b>{tier.title}</b> — от {price}/мес\n{features}")
    blocks.append(
        "Тариф действует на весь оплаченный период. Лимит устройств — условие "
        "оферты (п. 3.4). Чем дольше период, тем выгоднее."
    )
    return "\n\n".join(blocks)


BILLING_ERRORS = {
    "invalid_tier": "Этот тариф недоступен. Выберите другой.",
    "offer_not_accepted": "Сначала примите оферту: нажмите /start.",
    "banned": "⛔️ Доступ заблокирован.",
    "too_many_pending": "У вас слишком много неоплаченных счетов. Попробуйте позже.",
    "usdt_unavailable": "Оплата USDT временно недоступна.",
    "ruble_payments_unavailable": "Оплата через ЮKassa временно недоступна.",
    "provider_error": "Платёжный сервис не отвечает. Попробуйте через несколько минут.",
    "payment_processing": (
        "Заказ уже обрабатывается. Не оплачивайте повторно. "
        "Проверьте /status через минуту. Если срок не обновился, "
        "обратитесь в поддержку."
    ),
}

EMAIL_PROMPT = (
    "📧 Укажите email для кассового чека (54-ФЗ). Он нужен только для отправки "
    "чека и хранится в вашем профиле. Изменить его можно командой /email.\n\n"
    "Отмена: /cancel"
)


class ReceiptEmailState(StatesGroup):
    waiting = State()


async def show_channels(
    message: Message, months: int | None = None, tier: str | None = None
) -> None:
    await message.answer(CHANNEL_TEXT, reply_markup=channel_kb(months, tier))


def is_usdt_payment_request(text: str) -> bool:
    """Recognize direct USDT payment questions without catching ordinary mentions."""
    normalized = " ".join(text.casefold().split())
    if not _USDT_MENTION.search(normalized):
        return False
    if re.fullmatch(
        r"(?:usdt|crypto\s*pay|cryptobot|крипто(?:валют\w*|оплат\w*))[.!?…\s]*",
        normalized,
    ):
        return True
    return bool(_USDT_PAYMENT_INTENT.search(normalized))


async def show_usdt_options(message: Message, services: Services) -> None:
    if message.from_user is None:
        return
    if services.cryptopay is None:
        await message.answer(BILLING_ERRORS["usdt_unavailable"])
        return
    if not await services.db.has_accepted_offer(message.from_user.id):
        await message.answer(BILLING_ERRORS["offer_not_accepted"])
        return
    await message.answer(
        "Для оплаты подписки в USDT выберите тариф и срок. Затем бот покажет "
        "счёт Crypto Pay и QR-код. Счёт действует 60 минут. "
        "Продолжая, вы подтверждаете, что находитесь за пределами России.",
        reply_markup=tiers_kb("usdt", services.settings.enabled_tiers),
    )


@router.message(F.text == BTN_PAY)
async def menu_pay(message: Message) -> None:
    await show_channels(message)


@router.message(StateFilter(None), Command("usdt"))
async def cmd_usdt(message: Message, services: Services) -> None:
    """Separate entry point, intentionally absent from the ordinary payment menu."""
    await show_usdt_options(message, services)


@router.message(StateFilter(None), F.text.func(is_usdt_payment_request))
async def message_usdt_request(message: Message, services: Services) -> None:
    """Offer the private invoice flow when a user directly asks about USDT."""
    await show_usdt_options(message, services)


@router.message(Command("email"))
async def cmd_email(message: Message, state: FSMContext) -> None:
    await state.set_state(ReceiptEmailState.waiting)
    await state.update_data(method=None, months=None, tier=None)
    await message.answer(EMAIL_PROMPT)


@router.message(ReceiptEmailState.waiting, Command("cancel"))
async def email_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(ReceiptEmailState.waiting, F.text)
async def email_entered(
    message: Message, state: FSMContext, services: Services
) -> None:
    if message.from_user is None or message.text is None:
        return
    email = message.text.strip()
    if not is_valid_email(email):
        await message.answer("Похоже, это не email. Пример: name@example.com")
        return
    await services.db.set_receipt_email(message.from_user.id, email)
    data = await state.get_data()
    await state.clear()
    await message.answer("✅ Email для чеков сохранён.")
    method, months = data.get("method"), data.get("months")
    tier = str(data.get("tier") or DEFAULT_TIER)
    if method == "sbp" and isinstance(months, int):
        await create_and_show(
            message, message.from_user.id, method, months, services, state, tier
        )


@router.callback_query(F.data.startswith("chan:"))
async def cb_channel(
    callback: CallbackQuery, services: Services, state: FSMContext
) -> None:
    if not isinstance(callback.message, Message) or callback.data is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if parts[1] == "back":
        await callback.message.edit_text(CHANNEL_TEXT, reply_markup=channel_kb())
        await callback.answer()
        return
    method = parts[1]
    if method not in ("sbp", "usdt"):
        await callback.answer()
        return
    enabled = services.settings.enabled_tiers
    tier = parts[2] if len(parts) >= 3 else None
    if tier is not None and (tier not in TIERS or tier not in enabled):
        await callback.answer()
        return
    if tier and len(parts) == 4 and parts[3].isdigit():
        months = int(parts[3])
        if months in TIER_PRICES_RUB[tier]:
            await callback.answer()
            await create_and_show(
                callback.message,
                callback.from_user.id,
                method,
                months,
                services,
                state,
                tier,
            )
            return
    title = "рубли" if method == "sbp" else "USDT"
    if tier:
        await callback.message.edit_text(
            f"Выберите срок аренды ({TIERS[tier].title}, {title}):",
            reply_markup=tariffs_kb(method, tier),
        )
    else:
        await callback.message.edit_text(
            tiers_text(title, enabled), reply_markup=tiers_kb(method, enabled)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tier:"))
async def cb_tier(callback: CallbackQuery, services: Services) -> None:
    if not isinstance(callback.message, Message) or callback.data is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in ("sbp", "usdt"):
        await callback.answer()
        return
    method, tier = parts[1], parts[2]
    if tier not in TIERS or tier not in services.settings.enabled_tiers:
        await callback.answer()
        return
    title = "рубли" if method == "sbp" else "USDT"
    await callback.message.edit_text(
        f"Выберите срок аренды ({TIERS[tier].title}, {title}):",
        reply_markup=tariffs_kb(method, tier),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(
    callback: CallbackQuery, services: Services, state: FSMContext
) -> None:
    if not isinstance(callback.message, Message) or callback.data is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    # "pay:<method>:<months>" comes from messages sent before tiers existed.
    if len(parts) == 3:
        _, method, months_raw = parts
        tier = DEFAULT_TIER
    elif len(parts) == 4:
        _, method, tier, months_raw = parts
    else:
        await callback.answer()
        return
    if method not in ("sbp", "usdt") or not months_raw.isdigit() or tier not in TIERS:
        await callback.answer()
        return
    await callback.answer()
    await create_and_show(
        callback.message,
        callback.from_user.id,
        method,
        int(months_raw),
        services,
        state,
        tier,
    )


async def create_and_show(
    message: Message,
    telegram_id: int,
    method: str,
    months: int,
    services: Services,
    state: FSMContext,
    tier: str = DEFAULT_TIER,
) -> None:
    try:
        payment = await services.billing.create(telegram_id, months, method, tier)
    except BillingError as exc:
        if exc.code == "receipt_email_required":
            await state.set_state(ReceiptEmailState.waiting)
            await state.update_data(method=method, months=months, tier=tier)
            await message.answer(EMAIL_PROMPT)
            return
        await message.answer(BILLING_ERRORS.get(exc.code, "Не удалось создать платёж."))
        return
    data = payment.data
    plan = f"Тариф: <b>{TIERS[tier].title}</b>, срок: <b>{months} мес.</b>"
    if method == "sbp" and data.get("provider") == "yookassa":
        await message.answer(
            f"🇷🇺 <b>Оплата в рублях</b>\n\n"
            f"{plan}\nСумма: <b>{data['amount_rub']} ₽</b>\n"
            f"Номер платежа: <code>{payment.payment_id}</code>\n\n"
            "Нажмите кнопку ниже и оплатите через СБП или картой на странице ЮKassa. "
            "Узел продлится автоматически. "
            + (
                "Кассовый чек придёт на ваш email."
                if data.get("receipt") == "fiscal"
                else "Чек самозанятого формируется в «Мой налог» автоматически."
            ),
            reply_markup=yookassa_payment_kb(data["payment_url"]),
        )
    elif method == "sbp":
        manual_hint = (
            "После оплаты нажмите «✅ Я оплатил» и пришлите скриншот перевода в "
            "«💬 Поддержка». Подтверждение — обычно в течение часа."
            if data["manual"]
            else "Статус обновится автоматически после оплаты."
        )
        await message.answer_photo(
            BufferedInputFile(render_qr_png(data["payment_url"]), filename="sbp.png"),
            caption=(
                f"🇷🇺 <b>Оплата через СБП</b>\n\n"
                f"{plan}\nСумма: <b>{data['amount_rub']} ₽</b>\n"
                f"Номер платежа: <code>{payment.payment_id}</code>\n\n"
                "Отсканируйте QR-код в приложении банка "
                "или откройте ссылку кнопкой ниже. "
                "В комментарии к переводу укажите номер платежа.\n\n"
                f"{manual_hint}"
            ),
            reply_markup=sbp_payment_kb(data["payment_url"], payment.payment_id),
        )
    elif method == "usdt":
        pay_url = data.get("pay_url")
        if not isinstance(pay_url, str) or not is_valid_invoice_url(pay_url):
            logger.error("cryptopay_invoice_url_invalid")
            await message.answer(BILLING_ERRORS["provider_error"])
            return
        await message.answer(
            f"🌍 <b>Счёт на подписку в USDT</b>\n\n{plan}\n"
            f"Сумма: <b>{data['amount_usdt']} USDT</b>\n"
            f"Номер платежа: <code>{payment.payment_id}</code>\n\n"
            "Отсканируйте QR-код в CryptoBot или откройте счёт кнопкой ниже. "
            "Счёт действует 60 минут; доступ продлится после подтверждения оплаты.",
            reply_markup=usdt_payment_kb(pay_url),
        )
        await message.answer_photo(
            BufferedInputFile(render_qr_png(pay_url), filename="usdt-invoice.png"),
            caption="QR-код для открытия счёта Crypto Pay.",
        )


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(callback: CallbackQuery, services: Services) -> None:
    if not isinstance(callback.message, Message) or callback.data is None:
        await callback.answer()
        return
    payment_id = callback.data.split(":", 1)[1]
    payment = (
        await services.db.get_payment(payment_id)
        if is_valid_payment_id(payment_id)
        else None
    )
    if payment is None or payment["telegram_id"] != callback.from_user.id:
        await callback.answer("Платёж не найден", show_alert=True)
        return
    if payment["status"] != "pending":
        await callback.answer("Этот платёж уже обработан", show_alert=True)
        return
    await callback.answer("Спасибо! Ожидайте подтверждения")
    await callback.message.answer(
        "🕐 Платёж отправлен на проверку. Пришлите скриншот перевода в «💬 Поддержка», "
        "указав номер платежа: "
        f"<code>{payment_id}</code>",
    )
    user = callback.from_user
    await services.notifier.telegram(
        services.settings.admin_telegram_id,
        "💰 <b>Заявка на подтверждение СБП</b>\n"
        f"Пользователь: <code>{user.id}</code> @{user.username or '—'}\n"
        f"Сумма: {payment['amount'] // 100} ₽, {payment['months']} мес., "
        f"тариф {payment.get('tier', 'basic')}\n"
        f"Подтвердить: <code>/confirm {payment_id}</code>\n"
        "Не забудьте выдать чек сегодня: в «Мой налог» (НПД) "
        "или через онлайн-кассу (ИП).",
    )
