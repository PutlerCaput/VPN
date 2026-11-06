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
import uuid
from datetime import datetime
from typing import Any, Protocol

from aiogram.types import StarTransaction, TransactionPartnerUser

from db.database import Database
from services.provisioning import ProvisioningService
from services.security import is_valid_payment_id

logger = logging.getLogger(__name__)

CURRENCY = "XTR"
PROVIDER_PREFIX = "tgstars:"
# Invoice payload of a monthly subscription: "sub:<subscription id>".
SUBSCRIPTION_PREFIX = "sub:"
SUBSCRIPTION_PERIOD_SECONDS = 30 * 86400  # the only period Telegram allows
# A renewal that did not arrive this long after its date ends the subscription.
SUBSCRIPTION_GRACE_SECONDS = 3 * 86400
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

    async def edit_user_star_subscription(
        self, user_id: int, telegram_payment_charge_id: str, is_canceled: bool
    ) -> bool: ...


def _unix(value: Any) -> int:
    """aiogram parses dates into datetime; raw updates carry Unix seconds."""
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def subscription_id_from(payload: str) -> str | None:
    if not payload.startswith(SUBSCRIPTION_PREFIX):
        return None
    value = payload.removeprefix(SUBSCRIPTION_PREFIX)
    return value if is_valid_payment_id(value) else None


def subscription_payload(subscription_id: str) -> str:
    return f"{SUBSCRIPTION_PREFIX}{subscription_id}"


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
        subscription_id = subscription_id_from(payload)
        if subscription_id is not None:
            return await self._subscription_checkout_error(
                subscription_id, from_id, currency, total_amount
            )
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

    async def _subscription_checkout_error(
        self, subscription_id: str, from_id: int, currency: str, total_amount: int
    ) -> str | None:
        sub = await self.db.get_star_subscription(subscription_id)
        if sub is None or int(sub["telegram_id"]) != from_id:
            return "not_found"
        # "active"/"failed" too: whether Telegram asks before a renewal or a
        # retry is not documented, and neither may be refused by our own check.
        if sub["status"] not in ("pending", "active", "failed"):
            return "stale"
        if currency != CURRENCY or int(sub["amount"]) != total_amount:
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
        next_charge_at: int | None = None,
    ) -> bool:
        """Credit a completed Stars payment once. True if the order is now paid."""
        subscription_id = subscription_id_from(payload)
        if subscription_id is not None:
            return await self.record_subscription_charge(
                subscription_id=subscription_id,
                from_id=from_id,
                currency=currency,
                total_amount=total_amount,
                charge_id=charge_id,
                next_charge_at=next_charge_at,
            )
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

    async def record_subscription_charge(
        self,
        *,
        subscription_id: str,
        from_id: int,
        currency: str,
        total_amount: int,
        charge_id: str,
        next_charge_at: int | None,
    ) -> bool:
        """One subscription charge -> one payment -> one 30-day extension."""
        sub = await self.db.get_star_subscription(subscription_id)
        if (
            sub is None
            or int(sub["telegram_id"]) != from_id
            or currency != CURRENCY
            or int(sub["amount"]) != total_amount
            or not CHARGE_ID_RE.fullmatch(charge_id)
        ):
            logger.error("stars_subscription_mismatch")
            await self.provisioning.notifier.telegram(
                self.admin_telegram_id,
                "⚠️ Списание по подписке Stars не совпало с заказом. "
                f"Пользователь <code>{from_id}</code>, сумма {total_amount} XTR. "
                "Срок не продлён автоматически; проверьте и при необходимости "
                "оформите возврат.",
            )
            return False
        user = await self.db.get_user(from_id)
        if user is not None and user["status"] == "banned":
            await self.cancel_subscription(subscription_id)
            await self.provisioning.notifier.telegram(
                self.admin_telegram_id,
                f"⚠️ Заблокированному пользователю <code>{from_id}</code> пришло "
                "списание по подписке Stars. Автопродление отключено; срок не "
                f"продлён. Решите вопрос возврата: /refund после проверки "
                f"(списание <code>{charge_id}</code>).",
            )
            return False
        payment_id = await self.db.record_subscription_charge(
            subscription_id=subscription_id,
            payment_id=uuid.uuid4().hex,
            provider_id=provider_id_for(charge_id),
            next_charge_at=next_charge_at
            or int(time.time()) + SUBSCRIPTION_PERIOD_SECONDS,
        )
        await self.provisioning.process_paid_payment(payment_id)
        return True

    async def cancel_subscription(self, subscription_id: str) -> bool:
        """Stop future charges; the period already paid stays."""
        sub = await self.db.get_star_subscription(subscription_id)
        if sub is None:
            return False
        charges = [c for c in (sub["first_charge_id"], sub["last_charge_id"]) if c]
        for charge_id in dict.fromkeys(charges):
            try:
                await self.bot.edit_user_star_subscription(
                    user_id=int(sub["telegram_id"]),
                    telegram_payment_charge_id=str(charge_id),
                    is_canceled=True,
                )
                break
            except Exception as exc:
                logger.warning(
                    "stars_subscription_cancel_failed",
                    extra={"error": type(exc).__name__},
                )
        else:
            if charges:
                return False
        await self.db.set_star_subscription_status(subscription_id, "canceled")
        return True

    async def subscription_changed(
        self, *, payload: str, from_id: int, state: str
    ) -> str | None:
        """Telegram reported a subscription change (canceled/active/failed)."""
        subscription_id = subscription_id_from(payload)
        if subscription_id is None:
            return None
        sub = await self.db.get_star_subscription(subscription_id)
        if sub is None or int(sub["telegram_id"]) != from_id:
            return None
        status = {"canceled": "canceled", "active": "active", "failed": "failed"}.get(
            state
        )
        if status is None:
            return None
        await self.db.set_star_subscription_status(subscription_id, status)
        return status

    async def active_subscription(self, telegram_id: int) -> dict[str, Any] | None:
        for sub in await self.db.user_star_subscriptions(telegram_id):
            if sub["status"] in ("active", "failed"):
                return sub
        return None

    async def end_lapsed(self) -> int:
        return await self.db.end_lapsed_star_subscriptions(
            int(time.time()) - SUBSCRIPTION_GRACE_SECONDS
        )

    # ---------------------------------------------------------- reconciliation
    async def _reconcile_transaction(self, tx: StarTransaction) -> bool:
        source = tx.source
        if not isinstance(source, TransactionPartnerUser):
            return False  # outgoing refunds and withdrawals have no source user
        if source.transaction_type not in (None, "invoice_payment"):
            return False
        payload = source.invoice_payload or ""
        subscription_id = subscription_id_from(payload)
        if subscription_id is not None:
            if await self.db.get_payment_by_provider(provider_id_for(tx.id)):
                return False
            logger.warning("stars_subscription_charge_recovered")
            return await self.record_subscription_charge(
                subscription_id=subscription_id,
                from_id=source.user.id,
                currency=CURRENCY,
                total_amount=tx.amount,
                charge_id=tx.id,
                next_charge_at=_unix(tx.date) + SUBSCRIPTION_PERIOD_SECONDS,
            )
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
        if payment.get("subscription_id"):
            # A refunded renewal must not be followed by the next charge.
            await self.cancel_subscription(str(payment["subscription_id"]))
        return "refunded"

    async def record_refund(self, *, payload: str, from_id: int) -> bool:
        """Telegram reported a refund (refunded_payment service message)."""
        payment = await self._order(payload)
        if payment is None or int(payment["telegram_id"]) != from_id:
            return False
        return await self.provisioning.revoke_refunded_payment(payload) is not None
