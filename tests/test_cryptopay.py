from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from services.cryptopay import (
    INVOICE_DESCRIPTION,
    CryptoPayClient,
    CryptoPayError,
    is_valid_invoice_url,
    parse_paid_invoice,
    verify_webhook_signature,
)

TOKEN = "12345:TESTCRYPTOPAYTOKEN"


def sign(body: bytes, token: str = TOKEN) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_verify_webhook_signature_valid() -> None:
    body = b'{"update_type":"invoice_paid"}'
    assert verify_webhook_signature(TOKEN, body, sign(body))


def test_verify_webhook_signature_rejects_tampering() -> None:
    body = b'{"update_type":"invoice_paid","payload":{"amount":"3.50"}}'
    signature = sign(body)
    assert not verify_webhook_signature(
        TOKEN, body.replace(b"3.50", b"0.01"), signature
    )
    assert not verify_webhook_signature("other-token", body, signature)
    assert not verify_webhook_signature(TOKEN, body, None)
    assert not verify_webhook_signature(TOKEN, body, "deadbeef")


def test_verify_uses_sha256_of_token_not_raw_token() -> None:
    body = b"{}"
    raw_key_signature = hmac.new(TOKEN.encode(), body, hashlib.sha256).hexdigest()
    assert not verify_webhook_signature(TOKEN, body, raw_key_signature)


async def test_create_invoice_sends_expected_request() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["token"] = request.headers.get("Crypto-Pay-API-Token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "invoice_id": 42,
                    "bot_invoice_url": "https://t.me/CryptoBot?start=IV42",
                    "status": "active",
                },
            },
        )

    client = CryptoPayClient(
        TOKEN,
        "https://testnet-pay.crypt.bot/api",
        transport=httpx.MockTransport(handler),
    )
    invoice = await client.create_invoice(
        amount=Decimal("3.50"), telegram_id=777, months=1, payment_id="p" * 32
    )
    await client.close()

    assert invoice["invoice_id"] == 42
    assert invoice["pay_url"] == "https://t.me/CryptoBot?start=IV42"
    assert captured["path"] == "/api/createInvoice"
    assert captured["token"] == TOKEN
    body = captured["body"]
    assert body["asset"] == "USDT"
    assert body["amount"] == "3.50"
    assert body["description"] == INVOICE_DESCRIPTION
    assert body["expires_in"] == 3600
    payload = json.loads(body["payload"])
    assert payload["telegram_id"] == 777
    assert payload["months"] == 1
    assert payload["payment_id"] == "p" * 32
    assert len(payload["nonce"]) == 16


def test_invoice_url_must_use_an_allowed_https_host() -> None:
    assert is_valid_invoice_url("https://t.me/CryptoBot?start=IV42")
    assert is_valid_invoice_url("https://pay.crypt.bot/p?invoice_id=42")
    assert not is_valid_invoice_url("http://t.me/CryptoBot?start=IV42")
    assert not is_valid_invoice_url("https://example.com/invoice")
    assert not is_valid_invoice_url("https://t.me.evil.example/CryptoBot")
    assert not is_valid_invoice_url("https://user@t.me/CryptoBot")


async def test_create_invoice_error() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            400, json={"ok": False, "error": {"name": "AMOUNT_TOO_SMALL"}}
        ),
    )
    client = CryptoPayClient(TOKEN, transport=transport)
    with pytest.raises(CryptoPayError):
        await client.create_invoice(
            amount=Decimal("0.01"), telegram_id=1, months=1, payment_id="x" * 32
        )
    await client.close()


def test_parse_paid_invoice() -> None:
    assert parse_paid_invoice({"update_type": "other"}) is None
    assert (
        parse_paid_invoice(
            {"update_type": "invoice_paid", "payload": {"status": "active"}}
        )
        is None
    )
    invoice = {"invoice_id": 1, "status": "paid"}
    assert (
        parse_paid_invoice({"update_type": "invoice_paid", "payload": invoice})
        == invoice
    )
