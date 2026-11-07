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
from services.payment_creation import (
    CreationReviewRequired,
    CreationVoided,
    ReceiptEmailRequired,
    YooKassaCreationService,
)
from services.sbp import SbpService
from services.yookassa import YooKassaClient, YooKassaError, YooKassaRejected

logger = logging.getLogger(__name__)

MAX_PENDING_PAYMENTS = 1
# An unpaid Stars order of the same plan is reused for a repeated tap instead
# of filling the pending-order limit.
STARS_REUSE_SECONDS = 24 * 3600
METHODS = ("sbp", "usdt", "stars")
# Telegram's limit for a subscription price.
MAX_SUBSCRIPTION_STARS = 10000


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
            if not self.allow_legacy_sbp or not self.sbp.has_real_links:
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

    async def create_telegram_rub(
        self, telegram_id: int, months: int, tier: str
    ) -> CreatedPayment:
        """A ruble order paid inside Telegram through YooKassa (BotFather token)."""
        if tier not in TIERS or (
            self.enabled_tiers is not None and tier not in self.enabled_tiers
        ):
            raise BillingError("invalid_tier")
        if months not in TIER_PRICES_RUB[tier]:
            raise BillingError("invalid_months")
        user = await self.db.get_user(telegram_id)
        if user is None:
            raise BillingError("no_user")
        if user["status"] == "banned":
            raise BillingError("banned")
        if not await self.db.has_accepted_offer(telegram_id):
            raise BillingError("offer_not_accepted")
        amount = TIER_PRICES_RUB[tier][months] * 100
        existing = await self.db.open_telegram_rub_order(
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
            await self.db.create_telegram_rub_order(
                payment_id=payment_id,
                telegram_id=telegram_id,
                amount=amount,
                months=months,
                tier=tier,
            )
            PAYMENTS_CREATED.labels(method="sbp").inc()
            await audit(
                self.db,
                telegram_id,
                "payment_created",
                payment_id,
                method="yookassa_telegram",
                months=months,
                tier=tier,
            )
        title, description, label = invoice_texts(tier, months)
        return CreatedPayment(
            payment_id=payment_id,
            method="sbp",
            months=months,
            data={
                "amount_kopecks": amount,
                "title": title,
                "description": description,
                "label": label,
            },
        )

    async def create_stars_subscription(
        self, telegram_id: int, tier: str
    ) -> CreatedPayment:
        """A monthly Stars subscription (auto-renewal) for one tier.

        Returns the subscription ID as ``payment_id``; each charge later becomes
        its own payment. The price is the tier's one-month price in stars.
        """
        if self.stars_rub_per_star is None:
            raise BillingError("stars_unavailable")
        if tier not in TIERS or (
            self.enabled_tiers is not None and tier not in self.enabled_tiers
        ):
            raise BillingError("invalid_tier")
        if 1 not in TIER_PRICES_RUB[tier]:
            raise BillingError("invalid_months")
        user = await self.db.get_user(telegram_id)
        if user is None:
            raise BillingError("no_user")
        if user["status"] == "banned":
            raise BillingError("banned")
        if not await self.db.has_accepted_offer(telegram_id):
            raise BillingError("offer_not_accepted")
        subscriptions = await self.db.user_star_subscriptions(telegram_id)
        if any(sub["status"] in ("active", "failed") for sub in subscriptions):
            raise BillingError("subscription_exists")
        amount = stars_price(tier, 1, self.stars_rub_per_star)
        if amount > MAX_SUBSCRIPTION_STARS:
            raise BillingError("stars_unavailable")
        existing = await self.db.open_star_subscription(
            telegram_id, tier, int(time.time()) - STARS_REUSE_SECONDS
        )
        if existing is not None and int(existing["amount"]) == amount:
            subscription_id = str(existing["subscription_id"])
        else:
            subscription_id = uuid.uuid4().hex
            await self.db.create_star_subscription(
                subscription_id, telegram_id, tier, amount
            )
            await audit(
                self.db, telegram_id, "subscription_created", subscription_id, tier=tier
            )
        title, description, label = invoice_texts(tier, 1)
        return CreatedPayment(
            payment_id=subscription_id,
            method="stars",
            months=1,
            data={
                "amount_xtr": amount,
                "title": f"Автопродление: {TIERS[tier].title}"[:32],
                "description": (
                    f"{description} Звёзды списываются каждые 30 дней; "
                    "отключить можно в любой момент (/autorenew)."
                )[:255],
                "label": f"{label}, каждые 30 дней"[:64],
            },
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
        for attempt in range(3):
            # The first attempt keeps the customer's order id. An order that was
            # voided (it never got a link) is replaced by a fresh one, so the
            # customer gets a payment link at once.
            order_id = payment_id if attempt == 0 else uuid.uuid4().hex
            try:
                job = await creation.prepare(
                    payment_id=order_id,
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
                # A stuck order must not block the customer: void the stuck
                # orders and try once more.
                voided = await self.db.void_stuck_orders(
                    int(time.time()), self.yookassa.shop_id
                )
                if attempt == 0 and voided:
                    continue
                raise BillingError("too_many_pending") from None
            except CreationVoided:
                continue
            except CreationReviewRequired:
                raise BillingError("creation_review") from None
            except YooKassaError as exc:
                permanent = isinstance(exc, YooKassaRejected)
                logger.error(
                    "yookassa_create_failed",
                    extra={
                        "status_code": exc.status_code or 0,
                        "provider_code": exc.provider_code or "",
                        "failure": exc.failure or "",
                        "permanent": permanent,
                    },
                )
                code = "payment_method_unavailable" if permanent else "provider_error"
                raise BillingError(code) from None
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
        raise BillingError("provider_error")
