"""YooKassa payments inside Telegram (the token from @BotFather → Payments).

This is the integration the owner set up with YooKassa's official bot: the
bot sends an invoice in rubles with the provider token, Telegram shows the
YooKassa checkout (Russian bank cards, YooMoney, SberPay), and confirms the
payment with ``successful_payment``. No YooKassa REST keys, HTTP
notifications, public webhook or RU relay are involved, so none of them can
answer 404.

Flow mirrors Telegram Stars: the order is stored first, ``pre_checkout_query``
re-reads it, and the charge is bound once (unique provider ID) before the
shared exactly-once activation queue extends the subscription.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from db.database import Database
from services.provisioning import ProvisioningService
from services.security import is_valid_payment_id
from services.yookassa import receipt_item_description, rub_value

logger = logging.getLogger(__name__)

CURRENCY = "RUB"
PAYLOAD_PREFIX = "yk:"
# provider_payment_charge_id is YooKassa's payment number, the one the owner
# finds in the YooKassa dashboard (refunds are made there).
PROVIDER_PREFIX = "tgyk:"
CHECKOUT_MAX_AGE_SECONDS = 48 * 3600
CHARGE_ID_RE = re.compile(r"^[\x21-\x7e]{1,256}$")
TOKEN_RE = re.compile(r"^[0-9]{3,20}:[A-Za-z0-9_:-]{2,256}$")


def payload_for(payment_id: str) -> str:
    return f"{PAYLOAD_PREFIX}{payment_id}"


def payment_id_from(payload: str) -> str | None:
    if not payload.startswith(PAYLOAD_PREFIX):
        return None
    value = payload.removeprefix(PAYLOAD_PREFIX)
    return value if is_valid_payment_id(value) else None


def is_test_token(token: str) -> bool:
    return ":TEST:" in token.upper()


def invoice_options(
    *,
    receipt_mode: str,
    amount_kopecks: int,
    months: int,
    tier_title: str,
    vat_code: int,
    tax_system_code: int | None,
) -> dict[str, Any]:
    """Extra sendInvoice fields for 54-FZ receipts.

    Self-employed (НПД): YooKassa registers the receipt in «Мой налог» itself,
    nothing extra is sent. Fiscal (ИП/ООО with «Чеки от ЮKassa»): Telegram asks
    for the buyer's email and passes it to YooKassa together with the receipt.
    """
    if receipt_mode != "fiscal":
        return {}
    receipt: dict[str, Any] = {
        "items": [
            {
                "description": receipt_item_description(months, tier_title),
                "quantity": "1.00",
                "amount": {
                    "value": rub_value(amount_kopecks // 100),
                    "currency": "RUB",
                },
                "vat_code": vat_code,
                "payment_mode": "full_payment",
                "payment_subject": "service",
            }
        ]
    }
    if tax_system_code is not None:
        receipt["tax_system_code"] = tax_system_code
    return {
        "need_email": True,
        "send_email_to_provider": True,
        "provider_data": json.dumps({"receipt": receipt}, ensure_ascii=False),
    }


CHECKOUT_ERRORS = {
    "not_found": "Счёт не найден. Создайте новый в разделе «💳 Оплата».",
    "stale": "Счёт устарел. Создайте новый в разделе «💳 Оплата».",
    "already_paid": "Этот счёт уже оплачен. Проверьте срок командой /status.",
    "mismatch": "Условия счёта изменились. Создайте новый в разделе «💳 Оплата».",
    "banned": "Доступ заблокирован.",
    "offer_not_accepted": "Сначала примите оферту: нажмите /start.",
}


class TelegramRubPayments:
    def __init__(
        self,
        db: Database,
        provisioning: ProvisioningService,
        admin_telegram_id: int,
        provider_token: str,
    ) -> None:
        self.db = db
        self.provisioning = provisioning
        self.admin_telegram_id = admin_telegram_id
        self.provider_token = provider_token

    @property
    def test_mode(self) -> bool:
        return is_test_token(self.provider_token)

    async def _order(self, payload: str) -> dict[str, Any] | None:
        payment_id = payment_id_from(payload)
        if payment_id is None:
            return None
        payment = await self.db.get_payment(payment_id)
        if (
            payment is None
            or payment["method"] != "sbp"
            or payment.get("channel") != "telegram"
        ):
            return None
        return payment

    async def checkout_error(
        self, *, payload: str, from_id: int, currency: str, total_amount: int
    ) -> str | None:
        payment = await self._order(payload)
        if payment is None or int(payment["telegram_id"]) != from_id:
            return "not_found"
        if payment["status"] in ("paid", "refunded") or payment["provider_id"]:
            return "already_paid"
        if payment["status"] != "pending" or int(payment["created_at"]) < (
            int(time.time()) - CHECKOUT_MAX_AGE_SECONDS
        ):
            return "stale"
        if currency != CURRENCY or int(payment["amount"]) != total_amount:
            return "mismatch"
        user = await self.db.get_user(from_id)
        if user is None:
            return "not_found"
        if user["status"] == "banned":
            return "banned"
        if not await self.db.has_accepted_offer(from_id):
            return "offer_not_accepted"
        return None

    async def record_success(
        self,
        *,
        payload: str,
        from_id: int,
        currency: str,
        total_amount: int,
        provider_charge_id: str,
    ) -> bool:
        """Credit a completed YooKassa payment once. True if the order is paid."""
        payment = await self._order(payload)
        if (
            payment is None
            or int(payment["telegram_id"]) != from_id
            or currency != CURRENCY
            or int(payment["amount"]) != total_amount
            or not CHARGE_ID_RE.fullmatch(provider_charge_id)
        ):
            logger.error("telegram_rub_payment_mismatch")
            await self.provisioning.notifier.telegram(
                self.admin_telegram_id,
                "⚠️ Оплата ЮKassa в Telegram не совпала с заказом. Пользователь "
                f"<code>{from_id}</code>, сумма {total_amount / 100:.2f} ₽, платёж "
                f"ЮKassa <code>{provider_charge_id[:64]}</code>. Срок не продлён "
                "автоматически; проверьте платёж в личном кабинете ЮKassa.",
            )
            return False
        payment_id = str(payment["payment_id"])
        provider_id = f"{PROVIDER_PREFIX}{provider_charge_id}"
        if not await self.db.bind_telegram_rub_charge(payment_id, provider_id):
            logger.error("telegram_rub_charge_conflict")
            await self.provisioning.notifier.telegram(
                self.admin_telegram_id,
                f"⚠️ Заказ <code>{payment_id}</code> уже связан с другим платежом "
                "ЮKassa. Повторная оплата не зачислена; проверьте возврат в "
                "личном кабинете ЮKassa.",
            )
            return False
        await self.provisioning.process_paid_payment(payment_id)
        return True
