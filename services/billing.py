"""Payment creation shared by the bot and the REST API."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from config import (
    DAYS_PER_MONTH,
    DEFAULT_TIER,
    TIER_PRICES_RUB,
    TIER_PRICES_USDT,
    TIERS,
    format_gb,
    stars_price,
)
from db.database import Database, PendingPaymentLimitError
from services.audit import audit
from services.cryptopay import CryptoPayClient, CryptoPayError
from services.metrics import PAYMENTS_CREATED
from services.payment_creation import ReceiptEmailRequired, YooKassaCreationService
from services.sbp import SbpService
from services.yookassa import YooKassaClient, YooKassaError

logger = logging.getLogger(__name__)

MAX_PENDING_PAYMENTS = 5
# An unpaid Stars order of the same plan is reused for a repeated tap instead
# of filling the pending-order limit.
STARS_REUSE_SECONDS = 24 * 3600
METHODS = ("sbp", "usdt", "stars")


def invoice_texts(tier: str, months: int) -> tuple[str, str, str]:
    """(title <= 32, description <= 255, price label) for a Telegram invoice."""
    spec = TIERS[tier]
    title = f"Подписка {spec.title}, {months} мес."[:32]
    parts = [
        f"Защищённое подключение на {months * DAYS_PER_MONTH} дней",
        f"до {spec.devices} устройств",
    ]
    if spec.traffic_gb:
        parts.append(f"{format_gb(spec.traffic_bytes)} ГБ на каждые 30 дней")
    description = (", ".join(parts) + ". Условия — в оферте (/terms).")[:255]
    return title, description, f"{spec.title}, {months} мес."


class BillingError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class CreatedPayment:
    payment_id: str
    method: str
    months: int
    data: dict[str, Any] = field(default_factory=dict)


class BillingService:
    def __init__(
        self,
        db: Database,
        sbp: SbpService,
        cryptopay: CryptoPayClient | None,
        yookassa: YooKassaClient | None = None,
        enabled_tiers: list[str] | None = None,
        *,
        allow_legacy_sbp: bool = False,
        stars_rub_per_star: Decimal | None = None,
    ) -> None:
        self.db = db
        self.sbp = sbp
        self.cryptopay = cryptopay
        self.yookassa = yookassa
        # None = every defined tier is for sale (tests, scripts).
        self.enabled_tiers = enabled_tiers
        # Opt-in compatibility for old integrations/tests; normal application
        # construction never enables direct/manual ruble invoice creation.
        self.allow_legacy_sbp = allow_legacy_sbp
        # None = Telegram Stars are not sold.
        self.stars_rub_per_star = stars_rub_per_star

    async def create(
        self,
        telegram_id: int,
        months: int,
        method: str,
        tier: str = DEFAULT_TIER,
    ) -> CreatedPayment:
        if tier not in TIERS or (
            self.enabled_tiers is not None and tier not in self.enabled_tiers
        ):
            raise BillingError("invalid_tier")
        if months not in TIER_PRICES_RUB[tier]:
            raise BillingError("invalid_months")
        if method not in METHODS:
            raise BillingError("invalid_method")
        user = await self.db.get_user(telegram_id)
        if user is None:
            raise BillingError("no_user")
        if user["status"] == "banned":
            raise BillingError("banned")
        if not await self.db.has_accepted_offer(telegram_id):
            raise BillingError("offer_not_accepted")
        if method == "stars":
            return await self._create_stars(telegram_id, months, tier)
        if (
            not (method == "sbp" and self.yookassa is not None)
            and await self.db.count_pending_payments(telegram_id)
            >= MAX_PENDING_PAYMENTS
        ):
            raise BillingError("too_many_pending")

        payment_id = uuid.uuid4().hex
        data: dict[str, Any]
        if method == "sbp" and self.yookassa is not None:
            payment_id, data = await self._create_yookassa(
                payment_id, telegram_id, months, user, tier
            )
        elif method == "sbp":
            if not self.allow_legacy_sbp:
                raise BillingError("ruble_payments_unavailable")
            info = self.sbp.instructions(months, tier)
            await self.db.create_payment(
                payment_id=payment_id,
                telegram_id=telegram_id,
                amount=info.amount_rub * 100,
                currency="RUB",
                months=months,
                method="sbp",
                tier=tier,
            )
            data = {
                "qr_url": info.qr_url,
                "payment_url": info.payment_url,
                "amount_rub": info.amount_rub,
                "manual": self.sbp.is_manual,
                "provider": self.sbp.provider,
            }
        else:
            if self.cryptopay is None:
                raise BillingError("usdt_unavailable")
            amount: Decimal = TIER_PRICES_USDT[tier][months]
            try:
                invoice = await self.cryptopay.create_invoice(
                    amount=amount,
                    telegram_id=telegram_id,
                    months=months,
                    payment_id=payment_id,
                )
            except CryptoPayError:
                logger.exception("cryptopay_invoice_failed")
                raise BillingError("provider_error") from None
            invoice_id = str(invoice["invoice_id"])
            await self.db.create_payment(
                payment_id=payment_id,
                telegram_id=telegram_id,
                amount=int(amount * 100),
                currency="USDT",
                months=months,
                method="usdt",
                provider_id=f"cryptopay:{invoice_id}",
                tier=tier,
            )
            data = {
                "invoice_id": invoice_id,
                "pay_url": invoice.get("bot_invoice_url") or invoice.get("pay_url"),
                "amount_usdt": f"{amount:.2f}",
            }
        PAYMENTS_CREATED.labels(method=method).inc()
        await audit(
            self.db,
            telegram_id,
            "payment_created",
            payment_id,
            method=method,
            months=months,
            tier=tier,
        )
        return CreatedPayment(
            payment_id=payment_id, method=method, months=months, data=data
        )

    async def _create_stars(
        self, telegram_id: int, months: int, tier: str
    ) -> CreatedPayment:
        if self.stars_rub_per_star is None:
            raise BillingError("stars_unavailable")
        amount = stars_price(tier, months, self.stars_rub_per_star)
        existing = await self.db.open_stars_payment(
            telegram_id, months, tier, int(time.time()) - STARS_REUSE_SECONDS
        )
        if existing is not None and int(existing["amount"]) == amount:
            payment_id = str(existing["payment_id"])
        else:
            if (
                await self.db.count_pending_payments(telegram_id)
                >= MAX_PENDING_PAYMENTS
            ):
                raise BillingError("too_many_pending")
            payment_id = uuid.uuid4().hex
            await self.db.create_payment(
                payment_id=payment_id,
                telegram_id=telegram_id,
                amount=amount,
                currency="XTR",
                months=months,
                method="stars",
                tier=tier,
            )
            PAYMENTS_CREATED.labels(method="stars").inc()
            await audit(
                self.db,
                telegram_id,
                "payment_created",
                payment_id,
                method="stars",
                months=months,
                tier=tier,
            )
        title, description, label = invoice_texts(tier, months)
        return CreatedPayment(
            payment_id=payment_id,
            method="stars",
            months=months,
            data={
                "amount_xtr": amount,
                "title": title,
                "description": description,
                "label": label,
            },
        )

    async def _create_yookassa(
        self,
        payment_id: str,
        telegram_id: int,
        months: int,
        user: dict[str, Any],
        tier: str,
    ) -> tuple[str, dict[str, Any]]:
        assert self.yookassa is not None  # nosec B101
        creation = YooKassaCreationService(self.db, self.yookassa)
        try:
            job = await creation.prepare(
                payment_id=payment_id,
                telegram_id=telegram_id,
                amount_rub=TIER_PRICES_RUB[tier][months],
                months=months,
                email=user.get("receipt_email"),
                tier=tier,
                tier_title=TIERS[tier].title,
                max_pending=MAX_PENDING_PAYMENTS,
            )
            created = await creation.issue(job)
        except ReceiptEmailRequired:
            raise BillingError("receipt_email_required") from None
        except PendingPaymentLimitError:
            raise BillingError("too_many_pending") from None
        except YooKassaError as exc:
            logger.error("yookassa_create_failed", extra={"error": str(exc)})
            raise BillingError("provider_error") from None
        if not created["confirmation_url"]:
            # A completed invoice can legitimately have no checkout URL. Its
            # durable provider ID will be verified by the callback/poller.
            # Do not report an outage or suggest paying a second time.
            raise BillingError("payment_processing")
        return str(created["payment_id"]), {
            "qr_url": created["confirmation_url"],
            "payment_url": created["confirmation_url"],
            "amount_rub": int(created["amount"]) // 100,
            "manual": False,
            "provider": "yookassa",
            "receipt": created["receipt_mode"],
        }
