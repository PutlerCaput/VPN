"""Tariff selection and payment creation.

Inside Telegram, digital services are sold for Telegram Stars (XTR). The
YooKassa ruble checkout and the separate USDT entry point stay available
according to the owner's settings; see docs/PAYMENTS_TELEGRAM.md.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from config import DEFAULT_TIER, TIER_PRICES_RUB, TIERS
from keyboards.main_menu import (
    BTN_PAY,
    channel_kb,
    price_text,
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
from services.stars import CHECKOUT_ERRORS
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

METHOD_TITLES = {"sbp": "рубли", "usdt": "USDT", "stars": "Telegram Stars"}
NO_METHODS_TEXT = (
    "💳 Оплата временно недоступна. Напишите в «💬 Поддержка», "
    "мы сообщим, когда можно будет продлить подписку."
)


def channel_text(methods: list[str]) -> str:
    lines = ["💳 <b>Оплата подписки</b>"]
    if "stars" in methods:
        lines.append(
            "⭐ <b>Telegram Stars</b> — оплата внутри Telegram. Звёзды можно "
            "купить в самом Telegram; счёт откроется прямо в чате."
        )
    if "sbp" in methods:
        lines.append(
            "💳 <b>ЮKassa</b> — рубли через СБП или банковскую карту на странице "
            "ЮKassa. Доступные способы определяются платёжным сервисом и банком. "
            "Чек формируется в установленном порядке."
        )
    return "\n\n".join(lines)


def tiers_text(
    method: str, enabled: list[str], rub_per_star: Decimal = Decimal("1.60")
) -> str:
    """Tier overview: only what the service really provides today."""
    title = METHOD_TITLES.get(method, method)
    blocks = [f"⭐ <b>Выберите тариф</b> ({title})"]
    for key, tier in TIERS.items():
        if key not in enabled:
            continue
        features = "\n".join(f"• {item}" for item in tier.features)
        price = price_text(method, key, 1, rub_per_star)
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
    "stars_unavailable": "Оплата Telegram Stars временно недоступна.",
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


def bot_methods(services: Services) -> list[str]:
    """Methods of the ordinary payment menu that can actually take a payment."""
    billing = services.billing
    methods = []
    for method in services.settings.payment_methods_in_bot:
        if method == "stars" and billing.stars_rub_per_star is None:
            continue
        if (
            method == "sbp"
            and billing.yookassa is None
            and not billing.allow_legacy_sbp
        ):
            # Do not show a ruble button that can only answer "unavailable".
            continue
        methods.append(method)
    return methods


def method_available(method: str, services: Services) -> bool:
    if method == "usdt":
        return services.cryptopay is not None
    return method in bot_methods(services)


async def show_channels(
    message: Message,
    services: Services,
    months: int | None = None,
    tier: str | None = None,
) -> None:
    methods = bot_methods(services)
    if not methods:
        await message.answer(NO_METHODS_TEXT)
        return
    if tier is not None and tier not in services.settings.enabled_tiers:
        tier, months = None, None
    await message.answer(
        channel_text(methods), reply_markup=channel_kb(months, tier, methods)
    )


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
async def menu_pay(message: Message, services: Services) -> None:
    await show_channels(message, services)


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
    settings = services.settings
    if parts[1] == "back":
        methods = bot_methods(services)
        await callback.message.edit_text(
            channel_text(methods) if methods else NO_METHODS_TEXT,
            reply_markup=channel_kb(methods=methods),
        )
        await callback.answer()
        return
    method = parts[1]
    if method not in METHOD_TITLES or not method_available(method, services):
        await callback.answer("Этот способ оплаты сейчас недоступен.")
        return
    enabled = settings.enabled_tiers
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
    rate = settings.stars_rub_per_star
    if tier:
        await callback.message.edit_text(
            f"Выберите срок ({TIERS[tier].title}, {METHOD_TITLES[method]}):",
            reply_markup=tariffs_kb(method, tier, rate),
        )
    else:
        await callback.message.edit_text(
            tiers_text(method, enabled, rate),
            reply_markup=tiers_kb(method, enabled, rate),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tier:"))
async def cb_tier(callback: CallbackQuery, services: Services) -> None:
    if not isinstance(callback.message, Message) or callback.data is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in METHOD_TITLES:
        await callback.answer()
        return
    method, tier = parts[1], parts[2]
    if not method_available(method, services):
        await callback.answer("Этот способ оплаты сейчас недоступен.")
        return
    if tier not in TIERS or tier not in services.settings.enabled_tiers:
        await callback.answer()
        return
    await callback.message.edit_text(
        f"Выберите срок ({TIERS[tier].title}, {METHOD_TITLES[method]}):",
        reply_markup=tariffs_kb(method, tier, services.settings.stars_rub_per_star),
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
    if method not in METHOD_TITLES or not months_raw.isdigit() or tier not in TIERS:
        await callback.answer()
        return
    if not method_available(method, services):
        await callback.answer("Этот способ оплаты сейчас недоступен.")
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
    if method == "stars":
        await send_stars_invoice(message, payment.payment_id, data)
    elif method == "sbp" and data.get("provider") == "yookassa":
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


# ------------------------------------------------------------- Telegram Stars
async def send_stars_invoice(
    message: Message, payment_id: str, data: dict[str, object]
) -> None:
    amount = data.get("amount_xtr")
    if not isinstance(amount, int) or amount <= 0:
        await message.answer(BILLING_ERRORS["stars_unavailable"])
        return
    await message.answer_invoice(
        title=str(data["title"]),
        description=str(data["description"]),
        payload=payment_id,
        currency="XTR",
        prices=[LabeledPrice(label=str(data["label"]), amount=amount)],
    )
    await message.answer(
        f"Номер заказа: <code>{payment_id}</code>\n"
        "Нажмите «Оплатить» в счёте выше. Подписка продлится автоматически "
        "один раз сразу после подтверждения Telegram. "
        "Вопросы по оплате: /paysupport."
    )


@router.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery, services: Services) -> None:
    """Telegram cancels the payment unless this answers within 10 seconds."""
    error: str | None = "not_found"
    if services.stars is not None:
        try:
            error = await services.stars.checkout_error(
                payload=query.invoice_payload,
                from_id=query.from_user.id,
                currency=query.currency,
                total_amount=query.total_amount,
            )
        except Exception:
            logger.exception("stars_checkout_failed")
            error = "not_found"
    if error is None:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message=CHECKOUT_ERRORS.get(error, CHECKOUT_ERRORS["not_found"]),
        )


@router.message(F.successful_payment)
async def stars_successful_payment(message: Message, services: Services) -> None:
    paid = message.successful_payment
    if paid is None or message.from_user is None or services.stars is None:
        return
    if paid.currency != "XTR":
        logger.error("unexpected_payment_currency")
        return
    ok = await services.stars.record_success(
        payload=paid.invoice_payload,
        from_id=message.from_user.id,
        currency=paid.currency,
        total_amount=paid.total_amount,
        charge_id=paid.telegram_payment_charge_id,
    )
    if not ok:
        await message.answer(
            "⚠️ Оплата получена, но не совпала с заказом. Мы проверим её вручную "
            "и вернём звёзды или продлим подписку. Вопрос можно задать: /paysupport."
        )


@router.message(F.refunded_payment)
async def stars_refunded_payment(message: Message, services: Services) -> None:
    refunded = message.refunded_payment
    if refunded is None or message.from_user is None or services.stars is None:
        return
    await services.stars.record_refund(
        payload=refunded.invoice_payload, from_id=message.from_user.id
    )
