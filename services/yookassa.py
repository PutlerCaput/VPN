"""YooKassa (ЮKassa) API v3 client for RUB payments.

Two receipt modes:
- "self_employed" (default): the seller pays НПД and the shop is connected to
  ЮKassa as самозанятый. ЮKassa registers each payment in «Мой налог» itself;
  the payment description becomes the service name on the receipt, and no
  54-ФЗ receipt object is sent.
- "fiscal": an ИП/ООО with «Чеки от ЮKassa» or an online cash register. A 54-ФЗ
  receipt goes with each payment (tax_system_code=2 УСН «доходы», vat_code=1
  «без НДС» by default) and needs the buyer's email.

Notifications from YooKassa are not signed. The webhook therefore trusts only
the payment id from the body and re-reads the payment from the API with the
shop credentials before crediting anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Any

import httpx

API_BASE_URL = "https://api.yookassa.ru/v3"
RECEIPT_ITEM_DESCRIPTION = "Аренда удалённого технического узла, {months} мес."
RECEIPT_ITEM_TIER_DESCRIPTION = (
    "Аренда удалённого технического узла, тариф {tier}, {months} мес."
)
PAYMENT_DESCRIPTION = "Аренда узла, {months} мес. Платёж {payment_id}"


def receipt_item_description(months: int, tier_title: str | None) -> str:
    if tier_title:
        return RECEIPT_ITEM_TIER_DESCRIPTION.format(tier=tier_title, months=months)
    return RECEIPT_ITEM_DESCRIPTION.format(months=months)


# YooKassa payment ids are UUID-like: 8-4-4-4-12 hex characters.
YOOKASSA_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$"
)


class YooKassaError(Exception):
    pass


def is_valid_email(value: str) -> bool:
    return len(value) <= 254 and EMAIL_RE.fullmatch(value) is not None


def rub_value(amount_rub: int) -> str:
    return f"{amount_rub}.00"


@dataclass(frozen=True, slots=True)
class CreatedYooKassaPayment:
    yookassa_id: str
    confirmation_url: str


@dataclass(frozen=True, slots=True)
class VerifiedPayment:
    yookassa_id: str
    payment_id: str
    amount_kopecks: int


def notification_payment_id(update: Any) -> str | None:
    """Returns the YooKassa payment id from a payment.succeeded notification."""
    if not isinstance(update, dict) or update.get("event") != "payment.succeeded":
        return None
    obj = update.get("object")
    if not isinstance(obj, dict):
        return None
    yookassa_id = obj.get("id")
    if not isinstance(yookassa_id, str) or not YOOKASSA_ID_RE.fullmatch(yookassa_id):
        return None
    return yookassa_id


def verify_payment(data: Any) -> VerifiedPayment | None:
    """Accepts only a captured RUB payment carrying our payment_id in metadata."""
    if not isinstance(data, dict):
        return None
    if data.get("status") != "succeeded" or data.get("paid") is not True:
        return None
    amount = data.get("amount")
    metadata = data.get("metadata")
    yookassa_id = data.get("id")
    if not isinstance(amount, dict) or not isinstance(metadata, dict):
        return None
    if (
        amount.get("currency") != "RUB"
        or not isinstance(yookassa_id, str)
        or not YOOKASSA_ID_RE.fullmatch(yookassa_id)
    ):
        return None
    try:
        kopecks = Decimal(str(amount.get("value"))) * 100
    except DecimalException:
        return None
    if (
        not kopecks.is_finite()
        or kopecks <= 0
        or kopecks != kopecks.to_integral_value()
    ):
        return None
    payment_id = metadata.get("payment_id")
    if not isinstance(payment_id, str):
        return None
    return VerifiedPayment(
        yookassa_id=yookassa_id, payment_id=payment_id, amount_kopecks=int(kopecks)
    )


class YooKassaClient:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        return_url: str,
        receipt_mode: str = "self_employed",
        vat_code: int = 1,
        tax_system_code: int | None = 2,
        base_url: str = API_BASE_URL,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy_url: str | None = None,
    ) -> None:
        if receipt_mode not in ("self_employed", "fiscal"):
            raise ValueError("receipt_mode must be self_employed or fiscal")
        self.shop_id = shop_id
        self.return_url = return_url
        self.receipt_mode = receipt_mode
        self.vat_code = vat_code
        self.tax_system_code = tax_system_code
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            auth=httpx.BasicAuth(shop_id, secret_key),
            # CONNECT preserves end-to-end TLS to YooKassa. A failed gateway
            # raises an error; the client never retries over a direct route.
            proxy=proxy_url,
            follow_redirects=False,
        )

    @property
    def requires_email(self) -> bool:
        return self.receipt_mode == "fiscal"

    async def close(self) -> None:
        await self._client.aclose()

    def build_payment(
        self,
        *,
        payment_id: str,
        amount_rub: int,
        months: int,
        email: str | None,
        tier_title: str | None = None,
    ) -> dict[str, Any]:
        amount = {"value": rub_value(amount_rub), "currency": "RUB"}
        body: dict[str, Any] = {
            "amount": amount,
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self.return_url},
            "metadata": {"payment_id": payment_id},
        }
        if self.receipt_mode == "self_employed":
            # Becomes the service name on the «Мой налог» receipt.
            body["description"] = receipt_item_description(months, tier_title)
            return body
        if not email:
            raise YooKassaError("fiscal receipt needs the buyer's email")
        body["description"] = PAYMENT_DESCRIPTION.format(
            months=months, payment_id=payment_id
        )[:128]
        receipt: dict[str, Any] = {
            "customer": {"email": email},
            "items": [
                {
                    "description": receipt_item_description(months, tier_title),
                    "quantity": "1.00",
                    "amount": amount,
                    "vat_code": self.vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        }
        if self.tax_system_code is not None:
            receipt["tax_system_code"] = self.tax_system_code
        body["receipt"] = receipt
        return body

    async def create_payment(
        self,
        *,
        payment_id: str,
        amount_rub: int,
        months: int,
        email: str | None,
        tier_title: str | None = None,
    ) -> CreatedYooKassaPayment:
        body = self.build_payment(
            payment_id=payment_id,
            amount_rub=amount_rub,
            months=months,
            email=email,
            tier_title=tier_title,
        )
        # Our payment_id doubles as the idempotence key: a retried request
        # returns the same YooKassa payment instead of creating a second one.
        data = await self._request(
            "POST", "/payments", json=body, headers={"Idempotence-Key": payment_id}
        )
        confirmation = data.get("confirmation")
        yookassa_id = data.get("id")
        url = (
            confirmation.get("confirmation_url")
            if isinstance(confirmation, dict)
            else None
        )
        if not isinstance(yookassa_id, str) or not isinstance(url, str):
            raise YooKassaError("create_payment: unexpected response")
        return CreatedYooKassaPayment(yookassa_id=yookassa_id, confirmation_url=url)

    async def get_payment(self, yookassa_id: str) -> dict[str, Any]:
        if not YOOKASSA_ID_RE.fullmatch(yookassa_id):
            raise YooKassaError("get_payment: invalid id")
        return await self._request("GET", f"/payments/{yookassa_id}")

    async def submit_payment_request(
        self, payment_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Replay the exact persisted body and idempotency key within its lifetime."""
        return await self._request(
            "POST", "/payments", json=body, headers={"Idempotence-Key": payment_id}
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # Only the class name: the exception text may include the request URL.
            raise YooKassaError(
                f"{path}: transport error {type(exc).__name__}"
            ) from None
        if not 200 <= response.status_code < 300:
            raise YooKassaError(f"{path}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise YooKassaError(f"{path}: non-JSON body") from None
        if not isinstance(data, dict):
            raise YooKassaError(f"{path}: unexpected body")
        return data
