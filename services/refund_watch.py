"""Notice refunds made in the YooKassa dashboard and take the period back.

YooKassa does not tell the bot about a refund (the notification route is
retired) and the Bot API shows nothing for rubles, so the bot used to learn of
a refund only from the owner's /refund. The payment object keeps the sum that
went back in ``refunded_amount``; a paid ruble order is therefore asked about
from time to time: often in its first days, rarely later.

A full refund takes the paid period back exactly as /refund does. A partial one
only tells the owner: the offer returns "the unused part", not a fixed share,
so the decision is theirs.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, DecimalException
from typing import Any

from db.database import Database
from services.metrics import JOB_FAILURES
from services.notifier import Notifier
from services.provisioning import ProvisioningService, format_date
from services.yookassa import (
    YOOKASSA_ID_RE,
    YooKassaClient,
    YooKassaError,
    YooKassaRejected,
)

logger = logging.getLogger(__name__)

DAY = 86400
WATCH_DAYS = 400  # older paid orders are not asked about any more
MAX_PER_RUN = 20
# (order age up to, seconds between questions about it)
SCHEDULE = ((2 * DAY, 600), (14 * DAY, 3600), (WATCH_DAYS * DAY, DAY))
UNREACHABLE_RETRY = DAY


def check_interval(age: int) -> int:
    for limit, seconds in SCHEDULE:
        if age <= limit:
            return seconds
    return DAY


def refunded_kopecks(remote: dict[str, Any]) -> int:
    """The sum returned to the customer, in kopecks; 0 if none or unreadable."""
    amount = remote.get("refunded_amount")
    if not isinstance(amount, dict) or amount.get("currency") != "RUB":
        return 0
    try:
        kopecks = Decimal(str(amount.get("value"))) * 100
    except DecimalException:
        return 0
    if not kopecks.is_finite() or kopecks < 0 or kopecks != kopecks.to_integral_value():
        return 0
    return int(kopecks)


def rubles(kopecks: int) -> str:
    return f"{kopecks / 100:.2f}".replace(".", ",")


class RefundWatch:
    def __init__(
        self,
        db: Database,
        client: YooKassaClient | None,
        provisioning: ProvisioningService,
        notifier: Notifier,
        admin_telegram_id: int,
    ) -> None:
        self.db = db
        self.client = client
        self.provisioning = provisioning
        self.notifier = notifier
        self.admin_telegram_id = admin_telegram_id
        # Not persisted: after a restart every order is asked about once.
        self._next: dict[str, float] = {}

    async def run(self) -> int:
        """Ask about the orders that are due. Returns the refunds applied."""
        if self.client is None:
            return 0
        now = time.time()
        orders = await self.db.paid_ruble_payments(int(now) - WATCH_DAYS * DAY)
        for gone in set(self._next) - {str(o["payment_id"]) for o in orders}:
            del self._next[gone]
        applied = asked = 0
        for payment in orders:
            payment_id = str(payment["payment_id"])
            if self._next.get(payment_id, 0.0) > now:
                continue
            if asked >= MAX_PER_RUN:
                break
            asked += 1
            age = int(now) - int(payment["paid_at"] or payment["created_at"])
            # Set before the request: a failing order cannot be asked about
            # again and again.
            self._next[payment_id] = now + check_interval(age)
            try:
                if await self._check(self.client, payment):
                    applied += 1
            except YooKassaRejected:
                # 403/404: the order belongs to another shop (the in-Telegram
                # checkout can be linked to one). /refund still works for it.
                self._next[payment_id] = now + UNREACHABLE_RETRY
                logger.info(
                    "refund_watch_unreachable", extra={"payment_id": payment_id}
                )
            except YooKassaError:
                JOB_FAILURES.labels(job="refund_watch_api").inc()
                logger.warning("refund_watch_api_unavailable")
            except Exception:
                JOB_FAILURES.labels(job="refund_watch_unexpected").inc()
                logger.exception(
                    "refund_watch_failed", extra={"payment_id": payment_id}
                )
        return applied

    async def _check(self, client: YooKassaClient, payment: dict[str, Any]) -> bool:
        yookassa_id = str(payment["provider_id"]).split(":", 1)[1]
        if not YOOKASSA_ID_RE.fullmatch(yookassa_id):
            return False  # not a YooKassa number
        remote = await client.get_payment(yookassa_id)
        if remote.get("id") != yookassa_id or remote.get("status") != "succeeded":
            return False
        returned = refunded_kopecks(remote)
        if returned <= 0:
            return False
        if returned < int(payment["amount"]):
            await self._partial(payment, returned)
            return False
        return await self._full(payment, returned)

    async def _full(self, payment: dict[str, Any], returned: int) -> bool:
        payment_id = str(payment["payment_id"])
        target = await self.provisioning.revoke_refunded_payment(payment_id)
        fresh = await self.db.get_payment(payment_id)
        if fresh is None or fresh["status"] != "refunded":
            return False
        tail = (
            f"Срок подписки уменьшен: теперь до {format_date(target)}."
            if target is not None
            else "Срок автоматически не менялся (оплата не была зачтена, "
            "пользователь забанен или панель не ответила): проверьте /user."
        )
        await self.notifier.telegram(
            self.admin_telegram_id,
            f"↩️ ЮKassa вернула платёж <code>{payment_id}</code> полностью "
            f"({rubles(returned)} ₽). "
            f"Пользователь <code>{payment['telegram_id']}</code>. {tail}",
        )
        return True

    async def _partial(self, payment: dict[str, Any], returned: int) -> None:
        payment_id = str(payment["payment_id"])
        key = f"refund_partial:{payment_id}:{returned}"
        if not await self.db.claim_meta(key):
            return
        delivered = await self.notifier.telegram(
            self.admin_telegram_id,
            f"↩️ ЮKassa вернула часть платежа <code>{payment_id}</code>: "
            f"{rubles(returned)} из {rubles(int(payment['amount']))} ₽. Срок подписки "
            "не менялся: оферта возвращает неиспользованную часть, решение за вами. "
            f"Чтобы снять весь оплаченный период, выполните "
            f"<code>/refund {payment_id}</code>.",
        )
        if not delivered:
            await self.db.forget_meta(key)  # asked about again, told again
