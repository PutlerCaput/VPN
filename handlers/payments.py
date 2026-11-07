"""Tariff selection and payment creation.

Inside Telegram, digital services are sold for Telegram Stars (XTR). The
YooKassa ruble checkout and the separate USDT entry point stay available
according to the owner's settings; see docs/PAYMENTS_TELEGRAM.md.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotSubscriptionUpdated,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from config import DEFAULT_TIER, TIER_PRICES_RUB, TIERS, savings_percent
from keyboards.main_menu import (
    BTN_PAY,
    autorenew_cancel_kb,
    channel_kb,
    price_text,
    sbp_payment_kb,
    subscription_link_kb,
    tariffs_kb,
    tiers_kb,
    usdt_payment_kb,
    yookassa_payment_kb,
)
from services.billing import BillingError
from services.container import Services
from services.cryptopay import is_valid_invoice_url
from services.payment_reconciliation import PaymentReconciler
from services.provisioning import format_date
from services.qr import render_qr_png
from services.security import SlidingWindowLimiter, is_valid_payment_id
from services.stars import (
    CHECKOUT_ERRORS,
    SUBSCRIPTION_PERIOD_SECONDS,
    subscription_payload,
)
from services.telegram_rub import CHECKOUT_ERRORS as RUB_CHECKOUT_ERRORS
from services.telegram_rub import payment_id_from as rub_payment_id_from
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
            "💳 <b>Оплата через СБП</b> — откроется приложение вашего банка. "
            "Карту вводить не нужно. Чек формируется в установленном порядке."
        )
    return "\n\n".join(lines)


def tiers_text(
    method: str, enabled: list[str], rub_per_star: Decimal = Decimal("1.60")
) -> str:
    """Tier overview: only what the service really provides today."""
    title = METHOD_TITLES.get(method, method)
    blocks = [f"🛡 <b>Выберите тариф</b> ({title})"]
    for key, tier in TIERS.items():
        if key not in enabled:
            continue
        features = "\n".join(f"✓ {item}" for item in tier.features)
        price = price_text(method, key, 1, rub_per_star)
        line = f"<b>{tier.title}</b> — {price} за месяц"
        best = max(TIER_PRICES_RUB[key], key=lambda m: savings_percent(key, m))
        if method == "sbp" and savings_percent(key, best) > 0:
            per_month = round(TIER_PRICES_RUB[key][best] / best)
            line += (
                f"\nВыгоднее сразу на {best} мес.: ≈{per_month} ₽/мес, "
                f"экономия {savings_percent(key, best)}%"
            )
        blocks.append(f"{line}\n\n{features}")
    blocks.append(
        "Срок продлевается сразу после оплаты. Передумали — напишите в /paysupport, "
        "вернём деньги за неиспользованные дни (п. 6.3 оферты)."
    )
    return "\n\n".join(blocks)


BILLING_ERRORS = {
    "invalid_tier": "Этот тариф недоступен. Выберите другой.",
    "offer_not_accepted": "Сначала примите оферту: нажмите /start.",
    "banned": "⛔️ Доступ заблокирован.",
    "too_many_pending": (
        "У вас уже есть неоплаченный счёт. Оплатите его или отмените, чтобы "
        "выбрать другой срок. Неоплаченный счёт отменяется сам через час."
    ),
    "usdt_unavailable": "Оплата USDT временно недоступна.",
    "ruble_payments_unavailable": "Оплата через ЮKassa временно недоступна.",
    "stars_unavailable": "Оплата Telegram Stars временно недоступна.",
    "subscription_exists": (
        "Автопродление уже включено. Посмотреть или отключить: /autorenew."
    ),
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
            # Rubles go through SBP only: the YooKassa API with the SBP method,
            # never a Telegram invoice whose checkout asks for a bank card.
            and billing.yookassa is None
            and not (billing.allow_legacy_sbp and billing.sbp.has_real_links)
        ):
            # Do not show a ruble button that can only answer "unavailable".
            continue
        methods.append(method)
    return methods


def autorenew_available(services: Services) -> bool:
    return (
        services.settings.stars_autorenew_enabled
        and services.stars is not None
        and "stars" in bot_methods(services)
    )


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
            reply_markup=tariffs_kb(
                method, tier, rate, autorenew=autorenew_available(services)
            ),
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
        reply_markup=tariffs_kb(
            method,
            tier,
            services.settings.stars_rub_per_star,
            autorenew=autorenew_available(services),
        ),
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
        if exc.code == "too_many_pending":
            await show_open_payment(message, telegram_id, services)
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
            "Нажмите кнопку ниже: откроется приложение банка для оплаты через СБП. "
            "Узел продлится автоматически. "
            + (
                "Кассовый чек придёт на ваш email."
                if data.get("receipt") == "fiscal"
                else "Чек самозанятого формируется в «Мой налог» автоматически."
            ),
            reply_markup=yookassa_payment_kb(data["payment_url"], payment.payment_id),
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
    errors = CHECKOUT_ERRORS
    if rub_payment_id_from(query.invoice_payload) is not None:
        errors = RUB_CHECKOUT_ERRORS
        if services.telegram_rub is not None:
            try:
                error = await services.telegram_rub.checkout_error(
                    payload=query.invoice_payload,
                    from_id=query.from_user.id,
                    currency=query.currency,
                    total_amount=query.total_amount,
                )
            except Exception:
                logger.exception("telegram_rub_checkout_failed")
                error = "not_found"
    elif services.stars is not None:
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
            error_message=errors.get(error, errors["not_found"]),
        )


@router.message(F.successful_payment)
async def stars_successful_payment(message: Message, services: Services) -> None:
    paid = message.successful_payment
    if paid is None or message.from_user is None:
        return
    if paid.currency == "RUB" and services.telegram_rub is not None:
        credited = await services.telegram_rub.record_success(
            payload=paid.invoice_payload,
            from_id=message.from_user.id,
            currency=paid.currency,
            total_amount=paid.total_amount,
            provider_charge_id=paid.provider_payment_charge_id,
        )
        if not credited:
            await message.answer(
                "⚠️ Оплата получена, но не совпала с заказом. Мы проверим её "
                "вручную и продлим подписку или вернём деньги. Вопрос по оплате: "
                "/paysupport."
            )
        return
    if services.stars is None:
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
        next_charge_at=paid.subscription_expiration_date,
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


# ------------------------------------------------- monthly auto-renewal (Stars)
AUTORENEW_TERMS = (
    "Звёзды списываются автоматически каждые 30 дней, пока автопродление "
    "включено; каждое списание продлевает подписку на 30 дней. Отключить можно "
    "в любой момент командой /autorenew или в настройках Telegram — оплаченный "
    "срок сохранится. Условия — в оферте (п. 3.3.1, /terms)."
)


@router.callback_query(F.data.startswith("autorenew:"))
async def cb_autorenew(callback: CallbackQuery, services: Services) -> None:
    message = callback.message
    if not isinstance(message, Message) or callback.data is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) == 3 and parts[1] == "cancel":
        await autorenew_cancel(callback, message, parts[2], services)
        return
    tier = parts[1] if len(parts) == 2 else ""
    if (
        not autorenew_available(services)
        or tier not in TIERS
        or tier not in services.settings.enabled_tiers
    ):
        await callback.answer("Автопродление сейчас недоступно.")
        return
    await callback.answer()
    try:
        created = await services.billing.create_stars_subscription(
            callback.from_user.id, tier
        )
    except BillingError as exc:
        await message.answer(BILLING_ERRORS.get(exc.code, "Не удалось оформить."))
        return
    data = created.data
    link = await services.bot.create_invoice_link(
        title=str(data["title"]),
        description=str(data["description"]),
        payload=subscription_payload(created.payment_id),
        currency="XTR",
        prices=[LabeledPrice(label=str(data["label"]), amount=int(data["amount_xtr"]))],
        subscription_period=SUBSCRIPTION_PERIOD_SECONDS,
    )
    await message.answer(
        f"🔁 <b>Автопродление: {TIERS[tier].title}</b>\n\n"
        f"{data['amount_xtr']} ⭐ каждые 30 дней.\n\n{AUTORENEW_TERMS}",
        reply_markup=subscription_link_kb(link),
    )


async def autorenew_cancel(
    callback: CallbackQuery, message: Message, subscription_id: str, services: Services
) -> None:
    stars = services.stars
    sub = (
        await services.db.get_star_subscription(subscription_id)
        if stars is not None and is_valid_payment_id(subscription_id)
        else None
    )
    if stars is None or sub is None or int(sub["telegram_id"]) != callback.from_user.id:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await callback.answer()
    if await stars.cancel_subscription(subscription_id):
        await message.edit_reply_markup(reply_markup=None)
        await message.answer(
            "✅ Автопродление отключено. Звёзды больше не будут списываться; "
            "оплаченный срок сохраняется. Проверить: /status."
        )
    else:
        await message.answer(
            "⚠️ Telegram не подтвердил отключение. Попробуйте позже или отключите "
            "подписку в настройках Telegram; вопрос по оплате: /paysupport."
        )


@router.message(Command("autorenew"))
async def cmd_autorenew(message: Message, services: Services) -> None:
    if message.from_user is None:
        return
    stars = services.stars
    sub = (
        await stars.active_subscription(message.from_user.id)
        if stars is not None
        else None
    )
    if sub is None:
        hint = (
            "Включить: «💳 Оплата» → «⭐ Telegram Stars» → тариф → "
            "«🔁 Каждый месяц с автопродлением»."
            if autorenew_available(services)
            else "Автопродление сейчас не предлагается; продлевайте в «💳 Оплата»."
        )
        await message.answer(f"🔁 Автопродление не включено.\n\n{hint}")
        return
    tier = TIERS.get(str(sub["tier"]))
    title = tier.title if tier else str(sub["tier"])
    state = (
        "⚠️ последнее списание не прошло — пополните звёзды в Telegram"
        if sub["status"] == "failed"
        else "включено"
    )
    when = (
        f"\nСледующее списание: около {format_date(int(sub['next_charge_at']))}"
        if sub["next_charge_at"]
        else ""
    )
    await message.answer(
        f"🔁 <b>Автопродление {state}</b>\nТариф: {title}, "
        f"{sub['amount']} ⭐ каждые 30 дней.{when}",
        reply_markup=autorenew_cancel_kb(str(sub["subscription_id"])),
    )


@router.subscription()
async def stars_subscription_updated(
    update: BotSubscriptionUpdated, services: Services
) -> None:
    """Telegram reports when the user cancels, re-enables or a charge fails."""
    if services.stars is None:
        return
    status = await services.stars.subscription_changed(
        payload=update.invoice_payload, from_id=update.user.id, state=update.state
    )
    if status == "failed":
        await services.notifier.telegram(
            update.user.id,
            "⚠️ Не удалось списать звёзды за автопродление. Пополните баланс звёзд "
            "в Telegram или продлите подписку вручную в «💳 Оплата».",
        )


def is_yookassa_page(url: str) -> bool:
    """Only an HTTPS payment page of YooKassa/YooMoney is shown as a button."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == domain or host.endswith(f".{domain}")
        for domain in ("yoomoney.ru", "yookassa.ru")
    )


def open_payment_kb(payment_id: str, url: str | None) -> InlineKeyboardMarkup:
    rows = []
    if url and is_yookassa_page(url):
        rows.append([InlineKeyboardButton(text="💳 Оплатить через СБП", url=url)])
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату", callback_data=f"paycheck:{payment_id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отменить счёт", callback_data=f"paycancel:{payment_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_open_payment(
    message: Message, telegram_id: int, services: Services
) -> None:
    """One unpaid order at a time: show it with "pay" and "cancel"."""
    order = await services.db.open_payment(telegram_id)
    if order is None:
        await message.answer(BILLING_ERRORS["too_many_pending"])
        return
    tier = TIERS.get(str(order["tier"]))
    amount = (
        f"{order['amount']} ⭐"
        if order["currency"] == "XTR"
        else f"{order['amount'] // 100} ₽"
        if order["currency"] == "RUB"
        else f"{order['amount'] / 100:.2f} {order['currency']}"
    )
    await message.answer(
        "🧾 <b>У вас уже есть неоплаченный счёт</b>\n\n"
        f"Тариф: <b>{tier.title if tier else order['tier']}</b>, "
        f"срок: <b>{order['months']} мес.</b>, сумма: <b>{amount}</b>\n"
        f"Номер: <code>{order['payment_id']}</code>\n\n"
        "Оплатите его или отмените, чтобы выбрать другой срок. "
        "Неоплаченный счёт отменяется сам через час.",
        reply_markup=open_payment_kb(
            str(order["payment_id"]), order.get("confirmation_url")
        ),
    )


@router.callback_query(F.data.startswith("paycancel:"))
async def cb_cancel_payment(callback: CallbackQuery, services: Services) -> None:
    payment_id = (callback.data or "").removeprefix("paycancel:")
    if not is_valid_payment_id(payment_id) or callback.from_user is None:
        await callback.answer()
        return
    if not await services.db.cancel_own_payment(callback.from_user.id, payment_id):
        await callback.answer("Счёт уже оплачен, отменён или обрабатывается.")
        return
    await callback.answer("Счёт отменён")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await show_channels(callback.message, services)


# A customer may press "check" a few times; the provider is asked at most
# this often per person.
PAYCHECK_LIMIT = SlidingWindowLimiter(6, 60)
PAYCHECK_ANSWERS = {
    "paid": "✅ Оплата получена, подписка продлена.",
    "pending": (
        "⏳ Оплата пока не поступила. Если вы уже оплатили, подождите немного: "
        "бот пришлёт подтверждение сам."
    ),
    "cancelled": (
        "Счёт отменён. Если вы всё же оплатили его, бот зачислит оплату "
        "автоматически; вопросы — /paysupport."
    ),
    "not_found": "Счёт не найден.",
    "unavailable": "Платёжный сервис не ответил. Попробуйте через минуту.",
}


@router.callback_query(F.data.startswith("paycheck:"))
async def cb_check_payment(callback: CallbackQuery, services: Services) -> None:
    payment_id = (callback.data or "").removeprefix("paycheck:")
    if not is_valid_payment_id(payment_id) or callback.from_user is None:
        await callback.answer()
        return
    if not PAYCHECK_LIMIT.hit(str(callback.from_user.id)):
        await callback.answer("Слишком часто. Попробуйте через минуту.")
        return
    reconciler = PaymentReconciler(
        services.db,
        services.provisioning,
        services.billing.yookassa,
        services.cryptopay,
    )
    result = await reconciler.check_now(callback.from_user.id, payment_id)
    await callback.answer(PAYCHECK_ANSWERS[result], show_alert=result != "paid")
