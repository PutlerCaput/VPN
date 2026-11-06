"""Telegram Stars (XTR) payments for subscriptions sold inside the bot.

Telegram requires Stars for digital goods and services sold in bots
(https://core.telegram.org/bots/payments-stars). Flow:

1. ``BillingService.create(method="stars")`` stores a pending order with the
   price in whole stars; the handler sends an invoice whose payload is the
   order ID.
2. ``pre_checkout_query``: :meth:`StarsPayments.checkout_error` re-reads the
   order and must answer within 10 seconds.
3. ``successful_payment``: :meth:`StarsPayments.record_success` binds
   Telegram's charge ID to the order (unique, so a repeat cannot pay twice)
   and hands it to the same exactly-once activation queue as other payments.
4. :meth:`StarsPayments.reconcile` re-reads the bot's star transactions, so a
   payment whose update was lost or failed mid-way is still credited.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Protocol

from aiogram.types import StarTransaction, TransactionPartnerUser

from db.database import Database
from services.provisioning import ProvisioningService
from services.security import is_valid_payment_id

logger = logging.getLogger(__name__)

CURRENCY = "XTR"
PROVIDER_PREFIX = "tgstars:"
# An unpaid invoice older than this is refused at checkout: prices may have
# changed and housekeeping cancels such orders anyway.
CHECKOUT_MAX_AGE_SECONDS = 48 * 3600
CHARGE_ID_RE = re.compile(r"^[\x21-\x7e]{1,256}$")
TRANSACTIONS_PAGE = 100
MAX_PAGES = 20
RESCAN_OVERLAP = 50
META_SEEN = "stars_transactions_seen"

CHECKOUT_ERRORS = {
    "not_found": "Счёт не найден. Создайте новый в разделе «💳 Оплата».",
    "stale": "Счёт устарел. Создайте новый в разделе «💳 Оплата».",
    "already_paid": "Этот счёт уже оплачен. Проверьте срок командой /status.",
    "mismatch": "Условия счёта изменились. Создайте новый в разделе «💳 Оплата».",
    "banned": "Доступ заблокирован.",
    "offer_not_accepted": "Сначала примите оферту: нажмите /start.",
}


class StarsBot(Protocol):
    async def get_star_transactions(
        self, offset: int | None = None, limit: int | None = None
    ) -> Any: ...

    async def refund_star_payment(
        self, user_id: int, telegram_payment_charge_id: str
    ) -> bool: ...


def provider_id_for(charge_id: str) -> str:
    return f"{PROVIDER_PREFIX}{charge_id}"


class StarsPayments:
    def __init__(
        self,
        db: Database,
        provisioning: ProvisioningService,
        bot: StarsBot,
        admin_telegram_id: int,
    ) -> None:
        self.db = db
        self.provisioning = provisioning
        self.bot = bot
        self.admin_telegram_id = admin_telegram_id

    async def _order(self, payload: str) -> dict[str, Any] | None:
        if not is_valid_payment_id(payload):
            return None
        payment = await self.db.get_payment(payload)
        if payment is None or payment["method"] != "stars":
            return None
        return payment

    # --------------------------------------------------------------- checkout
    async def checkout_error(
        self, *, payload: str, from_id: int, currency: str, total_amount: int
    ) -> str | None:
        """None if the order may be charged now, else a CHECKOUT_ERRORS key."""
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

    # ---------------------------------------------------------------- success
    async def record_success(
        self,
        *,
        payload: str,
        from_id: int,
        currency: str,
        total_amount: int,
        charge_id: str,
    ) -> bool:
        """Credit a completed Stars payment once. True if the order is now paid."""
        payment = await self._order(payload)
        if (
            payment is None
            or int(payment["telegram_id"]) != from_id
            or currency != CURRENCY
            or int(payment["amount"]) != total_amount
            or not CHARGE_ID_RE.fullmatch(charge_id)
        ):
            logger.error("stars_payment_mismatch")
            await self.provisioning.notifier.telegram(
                self.admin_telegram_id,
                "⚠️ Получена оплата Stars, которая не совпала с заказом. "
                f"Пользователь <code>{from_id}</code>, сумма {total_amount} XTR. "
                "Доступ не выдан автоматически; проверьте и при необходимости "
                "оформите возврат.",
            )
            return False
        payment_id = str(payment["payment_id"])
        if not await self.db.bind_stars_charge(payment_id, provider_id_for(charge_id)):
            logger.error("stars_charge_conflict", extra={"payment_id": payment_id})
            await self.provisioning.notifier.telegram(
                self.admin_telegram_id,
                f"⚠️ Заказ <code>{payment_id}</code> уже связан с другим списанием "
                "Stars. Повторное списание не зачислено; проверьте возврат.",
            )
            return False
        await self.provisioning.process_paid_payment(payment_id)
        return True

    # ---------------------------------------------------------- reconciliation
    async def _reconcile_transaction(self, tx: StarTransaction) -> bool:
        source = tx.source
        if not isinstance(source, TransactionPartnerUser):
            return False  # outgoing refunds and withdrawals have no source user
        if source.transaction_type not in (None, "invoice_payment"):
            return False
        payload = source.invoice_payload or ""
        payment = await self._order(payload)
        if payment is None or payment["status"] in ("paid", "refunded"):
            return False
        if payment["provider_id"] not in (None, provider_id_for(tx.id)):
            return False
        logger.warning(
            "stars_payment_recovered", extra={"payment_id": payment["payment_id"]}
        )
        return await self.record_success(
            payload=payload,
            from_id=source.user.id,
            currency=CURRENCY,
            total_amount=tx.amount,
            charge_id=tx.id,
        )

    async def reconcile(self) -> int:
        """Credit paid orders found in the bot's star transactions."""
        seen = int(await self.db.get_meta(META_SEEN) or 0)
        credited = 0
        # The first page covers a newest-first listing; scanning on from the
        # last known position covers a chronological one.
        starts = [0, max(0, seen - RESCAN_OVERLAP)]
        total = seen
        for index, start in enumerate(starts):
            offset = start
            for _ in range(1 if index == 0 else MAX_PAGES):
                result = await self.bot.get_star_transactions(
                    offset=offset, limit=TRANSACTIONS_PAGE
                )
                transactions = list(getattr(result, "transactions", None) or [])
                for tx in transactions:
                    try:
                        credited += int(await self._reconcile_transaction(tx))
                    except Exception:
                        logger.exception("stars_reconcile_failed")
                offset += len(transactions)
                total = max(total, offset)
                if len(transactions) < TRANSACTIONS_PAGE:
                    break
        await self.db.set_meta(META_SEEN, str(total))
        return credited

    # ----------------------------------------------------------------- refund
    async def refund(self, payment_id: str) -> str:
        """Return the stars and take back the paid period (admin action)."""
        payment = await self._order(payment_id)
        if payment is None:
            return "not_found"
        provider_id = str(payment["provider_id"] or "")
        if payment["status"] != "paid" or not provider_id.startswith(PROVIDER_PREFIX):
            return "not_refundable"
        charge_id = provider_id.removeprefix(PROVIDER_PREFIX)
        try:
            await self.bot.refund_star_payment(
                user_id=int(payment["telegram_id"]),
                telegram_payment_charge_id=charge_id,
            )
        except Exception as exc:
            logger.error("stars_refund_failed", extra={"error": type(exc).__name__})
            return "telegram_error"
        await self.provisioning.revoke_refunded_payment(payment_id)
        return "refunded"

    async def record_refund(self, *, payload: str, from_id: int) -> bool:
        """Telegram reported a refund (refunded_payment service message)."""
        payment = await self._order(payload)
        if payment is None or int(payment["telegram_id"]) != from_id:
            return False
        return await self.provisioning.revoke_refunded_payment(payload) is not None
