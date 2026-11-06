"""Recover missed payment notifications using authenticated provider APIs.

No local timeout is proof of cancellation. API failures leave the order intact;
the existing provisioning outbox makes a poll/webhook race idempotent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal, DecimalException
from typing import Any, Literal

from db.database import Database
from services.cryptopay import CryptoPayClient, CryptoPayError
from services.metrics import JOB_FAILURES
from services.payment_creation import CreationReviewRequired, YooKassaCreationService
from services.provisioning import ProvisioningService
from services.yookassa import YooKassaClient, YooKassaError, verify_payment

logger = logging.getLogger(__name__)
CheckResult = Literal["paid", "cancelled", "pending", "mismatch"]
CHECK_LIMIT = 25
CHECK_CONCURRENCY = 3
CHECK_BUDGET_SECONDS = 45
CHECK_RETRY_SECONDS = 120


async def reconcile_yookassa(
    db: Database,
    client: YooKassaClient,
    provisioning: ProvisioningService,
    payment: dict[str, Any],
) -> CheckResult:
    provider_id = str(payment["provider_id"]).removeprefix("yookassa:")
    remote = await client.get_payment(provider_id)
    metadata = remote.get("metadata")
    if (
        remote.get("id") != provider_id
        or payment["currency"] != "RUB"
        or payment["method"] != "sbp"
        or not isinstance(metadata, dict)
        or metadata.get("payment_id") != payment["payment_id"]
    ):
        return "mismatch"
    if remote.get("status") == "canceled" and remote.get("paid") is False:
        await db.cancel_confirmed_payment(payment["payment_id"])
        return "cancelled"
    if remote.get("status") != "succeeded" or remote.get("paid") is not True:
        return "pending"
    verified = verify_payment(remote)
    if verified is None or verified.amount_kopecks < int(payment["amount"]):
        return "mismatch"
    await provisioning.process_paid_payment(payment["payment_id"])
    return "paid"


async def reconcile_cryptopay(
    db: Database,
    client: CryptoPayClient,
    provisioning: ProvisioningService,
    payment: dict[str, Any],
) -> CheckResult:
    invoice_id = str(payment["provider_id"]).removeprefix("cryptopay:")
    remote = await client.get_invoice(invoice_id)
    try:
        payload = json.loads(remote.get("payload", ""))
        amount = Decimal(str(remote.get("amount", ""))) * 100
    except (TypeError, ValueError, DecimalException):
        return "mismatch"
    if (
        str(remote.get("invoice_id")) != invoice_id
        or payment["method"] != "usdt"
        or payment["currency"] != "USDT"
        or remote.get("asset") != "USDT"
        or not amount.is_finite()
        or amount < int(payment["amount"])
        or not isinstance(payload, dict)
        or payload.get("payment_id") != payment["payment_id"]
        or payload.get("telegram_id") != payment["telegram_id"]
        or payload.get("months") != payment["months"]
    ):
        return "mismatch"
    if remote.get("status") == "expired":
        await db.cancel_confirmed_payment(payment["payment_id"])
        return "cancelled"
    if remote.get("status") != "paid":
        return "pending"
    await provisioning.process_paid_payment(payment["payment_id"])
    return "paid"


class PaymentReconciler:
    def __init__(
        self,
        db: Database,
        provisioning: ProvisioningService,
        yookassa: YooKassaClient | None,
        cryptopay: CryptoPayClient | None,
    ) -> None:
        self.db = db
        self.provisioning = provisioning
        self.yookassa = yookassa
        self.cryptopay = cryptopay

    async def poll(self) -> None:
        if self.yookassa is None and self.cryptopay is None:
            return
        payments = await self.db.payments_to_check(int(time.time()), CHECK_LIMIT)
        creations = (
            await self.db.yookassa_creations_to_retry(int(time.time()))
            if self.yookassa is not None
            else []
        )
        slots = asyncio.Semaphore(CHECK_CONCURRENCY)

        async def recover(job: dict[str, Any]) -> None:
            async with slots:
                if self.yookassa is None:
                    return
                try:
                    await YooKassaCreationService(self.db, self.yookassa).issue(job)
                except CreationReviewRequired:
                    JOB_FAILURES.labels(job="payment_creation_review").inc()
                    logger.error("payment_creation_review_required")
                except YooKassaError:
                    JOB_FAILURES.labels(job="payment_creation_api").inc()
                    logger.warning("payment_creation_api_unavailable")

        async def check(payment: dict[str, Any]) -> None:
            async with slots:
                # Persist before network I/O. A failing oldest invoice cannot
                # starve the queue, even if the process is restarted.
                await self.db.schedule_payment_check(
                    payment["payment_id"], int(time.time()) + CHECK_RETRY_SECONDS
                )
                try:
                    result: CheckResult = "pending"
                    provider = str(payment["provider_id"])
                    if provider.startswith("yookassa:") and self.yookassa is not None:
                        result = await reconcile_yookassa(
                            self.db, self.yookassa, self.provisioning, payment
                        )
                    elif (
                        provider.startswith("cryptopay:") and self.cryptopay is not None
                    ):
                        result = await reconcile_cryptopay(
                            self.db, self.cryptopay, self.provisioning, payment
                        )
                    if result == "mismatch":
                        JOB_FAILURES.labels(job="payment_reconciliation_mismatch").inc()
                        logger.error("payment_reconciliation_mismatch")
                except (YooKassaError, CryptoPayError):
                    JOB_FAILURES.labels(job="payment_reconciliation_api").inc()
                    logger.warning("payment_reconciliation_api_unavailable")

        try:
            async with asyncio.timeout(CHECK_BUDGET_SECONDS):
                async with asyncio.TaskGroup() as tasks:
                    for job in creations:
                        tasks.create_task(recover(job))
                    for payment in payments:
                        tasks.create_task(check(payment))
        except TimeoutError:
            JOB_FAILURES.labels(job="payment_reconciliation_timeout").inc()
            logger.warning("payment_reconciliation_timeout")
