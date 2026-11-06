"""Crypto Pay API (@CryptoBot) client and webhook verification."""

from __future__ import annotations

import hashlib
import json
import secrets
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from services.security import verify_hmac_sha256

INVOICE_DESCRIPTION = "Temporary lease of a remote technical node"
INVOICE_TTL_SECONDS = 3600


class CryptoPayError(Exception):
    pass


def is_valid_invoice_url(value: object) -> bool:
    """Only expose HTTPS invoice links hosted by Crypto Pay or Telegram."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"t.me", "pay.crypt.bot"}
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and bool(parsed.path)
    )


def verify_webhook_signature(
    api_token: str, raw_body: bytes, signature: str | None
) -> bool:
    """HMAC-SHA256(key=SHA256(api_token), msg=raw_body), checked before JSON parsing."""
    secret = hashlib.sha256(api_token.encode()).digest()
    return verify_hmac_sha256(secret, raw_body, signature)


def build_payload(telegram_id: int, months: int, payment_id: str) -> str:
    return json.dumps(
        {
            "telegram_id": telegram_id,
            "months": months,
            "payment_id": payment_id,
            "nonce": secrets.token_hex(8),
        },
        separators=(",", ":"),
    )


class CryptoPayClient:
    def __init__(
        self,
        api_token: str,
        base_url: str = "https://pay.crypt.bot/api",
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = api_token
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Crypto-Pay-API-Token": api_token},
        )

    @property
    def api_token(self) -> str:
        return self._token

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(f"/{method}", json=params)
        except httpx.HTTPError as exc:
            raise CryptoPayError(f"{method}: transport error {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise CryptoPayError(
                f"{method}: HTTP {response.status_code}, non-JSON body"
            ) from exc
        if not data.get("ok"):
            raise CryptoPayError(f"{method}: {data.get('error')}")
        return data["result"]

    async def create_invoice(
        self,
        *,
        amount: Decimal,
        telegram_id: int,
        months: int,
        payment_id: str,
    ) -> dict[str, Any]:
        result = await self._call(
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": "USDT",
                "amount": f"{amount:.2f}",
                "description": INVOICE_DESCRIPTION,
                "payload": build_payload(telegram_id, months, payment_id),
                "expires_in": INVOICE_TTL_SECONDS,
                "allow_comments": False,
                "allow_anonymous": True,
            },
        )
        if not isinstance(result, dict) or "invoice_id" not in result:
            raise CryptoPayError("createInvoice: malformed result")
        invoice_url = result.get("bot_invoice_url") or result.get("pay_url")
        if not is_valid_invoice_url(invoice_url):
            raise CryptoPayError("createInvoice: invalid invoice URL")
        result["pay_url"] = invoice_url
        return result

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        if not invoice_id.isascii() or not invoice_id.isdigit():
            raise CryptoPayError("getInvoices: invalid invoice id")
        result = await self._call("getInvoices", {"invoice_ids": invoice_id})
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise CryptoPayError("getInvoices: malformed result")
        for invoice in items:
            if (
                isinstance(invoice, dict)
                and str(invoice.get("invoice_id")) == invoice_id
            ):
                return invoice
        raise CryptoPayError("getInvoices: invoice not found")


def parse_paid_invoice(update: dict[str, Any]) -> dict[str, Any] | None:
    """Extracts the invoice from an invoice_paid update, None for anything else."""
    if update.get("update_type") != "invoice_paid":
        return None
    invoice = update.get("payload")
    if not isinstance(invoice, dict) or invoice.get("status") != "paid":
        return None
    return invoice
