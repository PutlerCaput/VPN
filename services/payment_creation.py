"""Durable YooKassa invoice creation with bounded idempotent recovery.

The order and immutable request are committed before a provider request. Unknown
outcomes are retried with the same body/key for at most 23 hours (YooKassa keeps
idempotency keys for 24 hours); older outcomes need operator reconciliation.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal, DecimalException
from typing import Any
from urllib.parse import urlsplit

from db.database import Database
from services.yookassa import YOOKASSA_ID_RE, YooKassaClient, YooKassaError

RETRY_WINDOW_SECONDS = 23 * 3600


class CreationReviewRequired(YooKassaError):
    pass


class ReceiptEmailRequired(YooKassaError):
    pass


def validated_response(
    response: dict[str, Any], request: dict[str, Any], payment_id: str
) -> tuple[str, str | None]:
    remote_id = response.get("id")
    metadata = response.get("metadata")
    amount = response.get("amount")
    if (
        not isinstance(remote_id, str)
        or not YOOKASSA_ID_RE.fullmatch(remote_id)
        or not isinstance(metadata, dict)
        or metadata.get("payment_id") != payment_id
        or not isinstance(amount, dict)
        or amount.get("currency") != "RUB"
    ):
        raise YooKassaError("creation response identity mismatch")
    try:
        remote_amount = Decimal(str(amount.get("value")))
        wanted_amount = Decimal(str(request["amount"]["value"]))
        if not remote_amount.is_finite() or remote_amount != wanted_amount:
            raise YooKassaError("creation response amount mismatch")
    except (DecimalException, KeyError, TypeError):
        raise YooKassaError("creation response invalid amount") from None
    confirmation = response.get("confirmation")
    url = (
        confirmation.get("confirmation_url") if isinstance(confirmation, dict) else None
    )
    # A completed/cancelled invoice can lack a checkout link. Preserve its
    # verified ID anyway so authenticated status reconciliation can finish it.
    if url is None:
        return remote_id, None
    if not isinstance(url, str) or len(url) > 4096:
        raise YooKassaError("creation response invalid confirmation URL")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        valid = False
    if not valid:
        raise YooKassaError("creation response invalid confirmation URL")
    return remote_id, url


class YooKassaCreationService:
    def __init__(self, db: Database, client: YooKassaClient) -> None:
        self.db = db
        self.client = client

    async def prepare(
        self,
        *,
        payment_id: str,
        telegram_id: int,
        amount_rub: int,
        months: int,
        tier: str,
        tier_title: str,
        email: str | None,
        max_pending: int,
    ) -> dict[str, Any]:
        existing = await self.db.open_yookassa_creation(telegram_id, months, tier)
        if existing is not None:
            return existing
        if self.client.requires_email and not email:
            raise ReceiptEmailRequired("receipt_email_required")
        body = self.client.build_payment(
            payment_id=payment_id,
            amount_rub=amount_rub,
            months=months,
            email=email,
            tier_title=tier_title,
        )
        return await self.db.prepare_yookassa_creation(
            payment_id=payment_id,
            telegram_id=telegram_id,
            amount=amount_rub * 100,
            months=months,
            tier=tier,
            shop_id=self.client.shop_id,
            request_body=json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            receipt_mode=self.client.receipt_mode,
            retry_until=int(time.time()) + RETRY_WINDOW_SECONDS,
            max_pending=max_pending,
        )

    async def issue(self, job: dict[str, Any]) -> dict[str, Any]:
        payment_id = str(job["payment_id"])
        current = await self.db.get_yookassa_creation(payment_id)
        if current is None:
            raise YooKassaError("creation request missing")
        job = current
        if job["shop_id"] != self.client.shop_id:
            await self.db.review_yookassa_creation(payment_id)
            raise CreationReviewRequired("creation merchant changed")
        if job["state"] == "ready":
            return job
        if job["state"] == "review" or int(time.time()) >= int(job["retry_until"]):
            await self.db.review_yookassa_creation(payment_id)
            raise CreationReviewRequired("creation idempotency window ended")
        if not await self.db.claim_yookassa_creation(payment_id, int(time.time())):
            # Another request may have completed while this caller waited for
            # the DB lock. Otherwise the existing lease/retry preserves its job.
            current = await self.db.get_yookassa_creation(payment_id)
            if current is not None and current["state"] == "ready":
                return current
            raise YooKassaError("creation already in progress")
        try:
            body: Any = json.loads(job["request_body"])
            if not isinstance(body, dict):
                raise YooKassaError("creation request malformed")
            response = await self.client.submit_payment_request(payment_id, body)
            provider_id, confirmation_url = validated_response(
                response, body, payment_id
            )
            await self.db.complete_yookassa_creation(
                payment_id, f"yookassa:{provider_id}", confirmation_url
            )
        finally:
            await self.db.release_yookassa_creation(payment_id)
        saved = await self.db.get_yookassa_creation(payment_id)
        if saved is None:
            raise YooKassaError("creation request disappeared")
        return saved
