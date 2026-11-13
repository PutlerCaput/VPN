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

Two ways to confirm an SBP payment:
- "redirect": YooKassa returns a link to its checkout page on yoomoney.ru. The
  page loads its scripts from YooMoney's CDN; where that CDN is unreachable the
  payer sees a blank page.
- "qr": YooKassa returns the SBP link itself (https://qr.nspk.ru/...). The bot
  draws the QR code and gives a button that opens the bank app; no YooMoney page
  is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Any
from urllib.parse import urlsplit

import httpx

API_BASE_URL = "https://api.yookassa.ru/v3"
# The service as the offer names it (legal/oferta.md, п. 1.1; a test keeps the two
# equal). It is the name on the receipt and the purpose of the payment that the
# customer's bank shows, and the one line to change when the wording changes. The
# tariff is deliberately not in it.
SERVICE_NAME = "Информационно-техническая услуга «Сервис защищённого доступа»"
RECEIPT_ITEM_DESCRIPTION = SERVICE_NAME + ", {months} мес."
# YooKassa takes at most 128 characters; the order number finds the payment.
PAYMENT_DESCRIPTION = RECEIPT_ITEM_DESCRIPTION + " Платёж {payment_id}"


def receipt_item_description(months: int) -> str:
    return RECEIPT_ITEM_DESCRIPTION.format(months=months)


# YooKassa payment ids are UUID-like: 8-4-4-4-12 hex characters.
YOOKASSA_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$"
)


class YooKassaError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
        failure: str | None = None,
    ) -> None:
        super().__init__(message)
        # Operational fields only, for the log: never the message text.
        self.status_code = status_code
        self.provider_code = provider_code
        self.failure = failure


class YooKassaRejected(YooKassaError):
    """YooKassa refused the request for good: it created nothing."""


# A retry cannot change these answers. 408, 425 and 429 are temporary, and 5xx
# leave the outcome unknown, so those requests are retried with the same key.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 422})


def _error_parts(response: httpx.Response) -> tuple[str | None, str]:
    """YooKassa's error code and a short text. Never the request or a secret."""
    try:
        body = response.json()
    except ValueError:
        return None, ""
    if not isinstance(body, dict):
        return None, ""
    code = body.get("code")
    code = (
        code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,60}", code) else None
    )
    text = body.get("description")
    clean = " ".join(text.split())[:160] if isinstance(text, str) else ""
    return code, clean


def is_valid_email(value: str) -> bool:
    return len(value) <= 254 and EMAIL_RE.fullmatch(value) is not None


# The SBP link YooKassa returns for the QR confirmation. Banks recognise it in
# a QR code, and on a phone it opens the bank app.
SBP_LINK_HOST = "qr.nspk.ru"
CONFIRMATION_TYPES = ("redirect", "qr")


def confirmation_link(confirmation: Any) -> Any:
    """The payer's link from a payment's confirmation, whatever its type.

    Not validated here: the caller checks it before storing or showing it.
    """
    if not isinstance(confirmation, dict):
        return None
    if confirmation.get("type") == "qr":
        return confirmation.get("confirmation_data")
    return confirmation.get("confirmation_url")


def is_sbp_link(url: str) -> bool:
    """An HTTPS SBP payment link of NSPK: shown as a QR code and a bank button."""
    if len(url) > 4096 or any(ch.isspace() or not ch.isprintable() for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() == SBP_LINK_HOST
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
        and len(parts.path) > 1
    )


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
        payment_method: str | None = None,
        confirmation: str = "redirect",
    ) -> None:
        if receipt_mode not in ("self_employed", "fiscal"):
            raise ValueError("receipt_mode must be self_employed or fiscal")
        if payment_method not in (None, "sbp"):
            raise ValueError("payment_method must be None or sbp")
        if confirmation not in CONFIRMATION_TYPES:
            raise ValueError("confirmation must be redirect or qr")
        if confirmation == "qr" and payment_method != "sbp":
            # The QR scenario is used here for SBP only: its link is an SBP link.
            raise ValueError("the qr confirmation needs payment_method sbp")
        # "sbp": the payer goes straight to the bank app, no card form.
        self.payment_method = payment_method
        self.confirmation = confirmation
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
    ) -> dict[str, Any]:
        amount = {"value": rub_value(amount_rub), "currency": "RUB"}
        body: dict[str, Any] = {
            "amount": amount,
            "capture": True,
            # For "qr" the bank app opens return_url after the payment.
            "confirmation": {"type": self.confirmation, "return_url": self.return_url},
            "metadata": {"payment_id": payment_id},
        }
        if self.payment_method is not None:
            body["payment_method_data"] = {"type": self.payment_method}
        if self.receipt_mode == "self_employed":
            # Becomes the service name on the «Мой налог» receipt.
            body["description"] = receipt_item_description(months)
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
                    "description": receipt_item_description(months),
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
    ) -> CreatedYooKassaPayment:
        body = self.build_payment(
            payment_id=payment_id,
            amount_rub=amount_rub,
            months=months,
            email=email,
        )
        # Our payment_id doubles as the idempotence key: a retried request
        # returns the same YooKassa payment instead of creating a second one.
        data = await self._request(
            "POST", "/payments", json=body, headers={"Idempotence-Key": payment_id}
        )
        yookassa_id = data.get("id")
        url = confirmation_link(data.get("confirmation"))
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
            failure = type(exc).__name__
            raise YooKassaError(
                f"{path}: transport error {failure}", failure=failure
            ) from None
        if not 200 <= response.status_code < 300:
            code, text = _error_parts(response)
            detail = f"{path}: HTTP {response.status_code}"
            if code:
                detail = f"{detail} {code}"
            if text:
                detail = f"{detail}: {text}"
            error_type = (
                YooKassaRejected
                if response.status_code in PERMANENT_STATUSES
                else YooKassaError
            )
            raise error_type(
                detail, status_code=response.status_code, provider_code=code
            )
        try:
            data = response.json()
        except ValueError:
            raise YooKassaError(f"{path}: non-JSON body") from None
        if not isinstance(data, dict):
            raise YooKassaError(f"{path}: unexpected body")
        return data
