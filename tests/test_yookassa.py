"""YooKassa: payment creation with a 54-ФЗ receipt and API-verified webhooks."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.billing import BillingError
from services.yookassa import (
    YooKassaClient,
    is_valid_email,
    notification_payment_id,
    verify_payment,
)
from tests.conftest import make_init_data
from webhook_server import create_webhook_app

SHOP_ID = "123456"
SECRET_KEY = "test_" + "k" * 40
YID = "2f3a9c1e-000f-5000-8000-1a2b3c4d5e6f"
CONFIRMATION_URL = "https://yoomoney.ru/checkout/payments/v2/contract?orderId=" + YID


class FakeYooKassaApi:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.remote: dict[str, Any] = {}
        self.fail_get = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        if request.method == "POST" and request.url.path == "/v3/payments":
            body = json.loads(request.content)
            self.created.append(body)
            self.remote = {
                "id": YID,
                "status": "pending",
                "paid": False,
                "amount": body["amount"],
                "metadata": body["metadata"],
            }
            return httpx.Response(
                200,
                json={
                    **self.remote,
                    "confirmation": {
                        "type": "redirect",
                        "confirmation_url": CONFIRMATION_URL,
                    },
                },
            )
        if request.method == "GET" and request.url.path == f"/v3/payments/{YID}":
            if self.fail_get:
                return httpx.Response(500, json={"type": "error"})
            return httpx.Response(200, json=self.remote)
        return httpx.Response(404, json={"type": "error"})

    def succeed(self, value: str | None = None) -> None:
        self.remote = {**self.remote, "status": "succeeded", "paid": True}
        if value is not None:
            self.remote["amount"] = {"value": value, "currency": "RUB"}


@pytest.fixture
def api() -> FakeYooKassaApi:
    return FakeYooKassaApi()


@pytest.fixture
def yookassa(api: FakeYooKassaApi) -> YooKassaClient:
    return YooKassaClient(
        SHOP_ID,
        SECRET_KEY,
        return_url="https://t.me/test_node_bot",
        receipt_mode="fiscal",
        base_url="https://api.yookassa.ru/v3",
        transport=httpx.MockTransport(api.handler),
    )


@pytest.fixture
def self_employed(api: FakeYooKassaApi) -> YooKassaClient:
    return YooKassaClient(
        SHOP_ID,
        SECRET_KEY,
        return_url="https://t.me/test_node_bot",
        base_url="https://api.yookassa.ru/v3",
        transport=httpx.MockTransport(api.handler),
    )


@pytest.fixture
def yk_services(services: Any, yookassa: YooKassaClient) -> Any:
    services.yookassa = yookassa
    services.billing.yookassa = yookassa
    return services


def notification(yookassa_id: str = YID, event: str = "payment.succeeded") -> bytes:
    return json.dumps(
        {
            "type": "notification",
            "event": event,
            "object": {"id": yookassa_id, "status": "succeeded", "paid": True},
        }
    ).encode()


async def _paying_user(services: Any, telegram_id: int) -> None:
    await services.db.create_user(telegram_id, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(telegram_id)


def test_email_validation() -> None:
    assert is_valid_email("name@example.com")
    assert not is_valid_email("name@example")
    assert not is_valid_email("not an email")
    assert not is_valid_email("a" * 300 + "@example.com")


def test_notification_parsing_accepts_only_succeeded_with_valid_id() -> None:
    assert notification_payment_id(json.loads(notification())) == YID
    assert (
        notification_payment_id(json.loads(notification(event="payment.canceled")))
        is None
    )
    assert notification_payment_id(json.loads(notification("../../admin"))) is None
    assert notification_payment_id(["not", "a", "dict"]) is None


def test_verify_payment_requires_captured_rub() -> None:
    good = {
        "id": YID,
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "300.00", "currency": "RUB"},
        "metadata": {"payment_id": "p" * 32},
    }
    verified = verify_payment(good)
    assert verified is not None
    assert verified.amount_kopecks == 30000
    assert verify_payment({**good, "status": "pending"}) is None
    assert verify_payment({**good, "paid": False}) is None
    assert (
        verify_payment({**good, "amount": {"value": "300.00", "currency": "USD"}})
        is None
    )
    assert (
        verify_payment({**good, "amount": {"value": "abc", "currency": "RUB"}}) is None
    )
    assert verify_payment({**good, "metadata": {}}) is None


async def test_create_payment_sends_receipt_and_idempotence_key(
    yookassa: YooKassaClient, api: FakeYooKassaApi
) -> None:
    created = await yookassa.create_payment(
        payment_id="a" * 32, amount_rub=800, months=3, email="buyer@example.com"
    )
    await yookassa.close()
    assert created.yookassa_id == YID
    assert created.confirmation_url == CONFIRMATION_URL
    body = api.created[0]
    assert body["amount"] == {"value": "800.00", "currency": "RUB"}
    assert body["capture"] is True
    assert body["metadata"] == {"payment_id": "a" * 32}
    receipt = body["receipt"]
    assert receipt["customer"] == {"email": "buyer@example.com"}
    assert receipt["tax_system_code"] == 2
    item = receipt["items"][0]
    assert item["vat_code"] == 1
    assert item["payment_subject"] == "service"
    assert item["payment_mode"] == "full_payment"
    assert item["amount"] == body["amount"]
    headers = api.headers[0]
    assert headers["Idempotence-Key"] == "a" * 32
    expected = base64.b64encode(f"{SHOP_ID}:{SECRET_KEY}".encode()).decode()
    assert headers["Authorization"] == f"Basic {expected}"


async def test_billing_requires_receipt_email(yk_services: Any) -> None:
    await _paying_user(yk_services, 990001)
    with pytest.raises(BillingError) as exc:
        await yk_services.billing.create(990001, 1, "sbp")
    assert exc.value.code == "receipt_email_required"
    await yk_services.db.set_receipt_email(990001, "buyer@example.com")
    payment = await yk_services.billing.create(990001, 1, "sbp")
    assert payment.data["provider"] == "yookassa"
    assert payment.data["payment_url"] == CONFIRMATION_URL
    stored = await yk_services.db.get_payment(payment.payment_id)
    assert stored["provider_id"] == f"yookassa:{YID}"
    assert stored["amount"] == 30000


async def test_webhook_credits_only_after_api_confirms(
    yk_services: Any, api: FakeYooKassaApi
) -> None:
    await _paying_user(yk_services, 990002)
    await yk_services.db.set_receipt_email(990002, "buyer@example.com")
    payment = await yk_services.billing.create(990002, 1, "sbp")
    expires_before = (await yk_services.db.get_user(990002))["expires_at"]
    app = create_webhook_app(yk_services.bot, None, yk_services)  # type: ignore[arg-type]

    async with TestClient(TestServer(app)) as client:
        # Forged/early "succeeded" while the API still says pending:
        # do not credit; ask YooKassa to retry after its API state catches up.
        response = await client.post("/yookassa/webhook", data=notification())
        assert response.status == 503
        assert (await yk_services.db.get_payment(payment.payment_id))[
            "status"
        ] == "pending"

        # Paid less than the tariff: ignored.
        api.succeed("1.00")
        await client.post("/yookassa/webhook", data=notification())
        assert (await yk_services.db.get_payment(payment.payment_id))[
            "status"
        ] == "pending"

        # API unavailable: 503 so YooKassa retries later.
        api.succeed("300.00")
        api.fail_get = True
        response = await client.post("/yookassa/webhook", data=notification())
        assert response.status == 503
        api.fail_get = False

        response = await client.post("/yookassa/webhook", data=notification())
        assert response.status == 200
        assert (await yk_services.db.get_payment(payment.payment_id))[
            "status"
        ] == "paid"
        expires_after = (await yk_services.db.get_user(990002))["expires_at"]
        assert expires_after - expires_before == 30 * 86400

        # A repeated notification does not extend twice.
        await client.post("/yookassa/webhook", data=notification())
        assert (await yk_services.db.get_user(990002))["expires_at"] == expires_after

        # Unknown payment and garbage bodies.
        unknown = await client.post(
            "/yookassa/webhook",
            data=notification("00000000-0000-4000-8000-000000000000"),
        )
        assert unknown.status == 503
        assert (await client.post("/yookassa/webhook", data=b"{not json")).status == 400


async def test_webhook_disabled_without_yookassa(services: Any) -> None:
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/yookassa/webhook", data=notification())
        assert response.status == 404


async def test_api_asks_for_receipt_email(yk_services: Any) -> None:
    bot_token = yk_services.settings.bot_token.get_secret_value()
    app = create_webhook_app(yk_services.bot, None, yk_services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        auth = await client.post(
            "/api/v1/auth/telegram",
            json={"initData": make_init_data(bot_token, 990003)},
        )
        headers = {"Authorization": f"Bearer {(await auth.json())['access_token']}"}
        assert (
            await client.post("/api/v1/legal/accept", headers=headers)
        ).status == 200

        body: dict[str, Any] = {"months": 1, "method": "sbp"}
        response = await client.post(
            "/api/v1/payments/create", json=body, headers=headers
        )
        assert response.status == 409
        assert (await response.json())["error"] == "receipt_email_required"

        bad = await client.post(
            "/api/v1/payments/create", json={**body, "email": "nope"}, headers=headers
        )
        assert bad.status == 400

        ok = await client.post(
            "/api/v1/payments/create",
            json={**body, "email": "buyer@example.com"},
            headers=headers,
        )
        assert ok.status == 201
        data = await ok.json()
        assert data["provider"] == "yookassa"
        assert data["payment_url"] == CONFIRMATION_URL


async def test_self_employed_payment_has_no_fiscal_receipt(
    self_employed: YooKassaClient, api: FakeYooKassaApi
) -> None:
    assert self_employed.receipt_mode == "self_employed"
    assert not self_employed.requires_email
    await self_employed.create_payment(
        payment_id="b" * 32, amount_rub=300, months=1, email=None
    )
    await self_employed.close()
    body = api.created[0]
    assert "receipt" not in body
    # The description is the service name on the «Мой налог» receipt.
    assert body["description"] == "Аренда удалённого технического узла, 1 мес."
    assert body["metadata"] == {"payment_id": "b" * 32}


async def test_self_employed_billing_needs_no_email(
    services: Any, self_employed: YooKassaClient
) -> None:
    services.yookassa = self_employed
    services.billing.yookassa = self_employed
    await _paying_user(services, 990004)
    payment = await services.billing.create(990004, 3, "sbp")
    assert payment.data["provider"] == "yookassa"
    assert payment.data["receipt"] == "self_employed"
    stored = await services.db.get_payment(payment.payment_id)
    assert stored["amount"] == 80000


def test_unknown_receipt_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="receipt_mode"):
        YooKassaClient(
            SHOP_ID, SECRET_KEY, return_url="https://t.me/x", receipt_mode="npd"
        )
