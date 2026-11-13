"""Persistent invoice creation survives an unknown remote outcome and a restart."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from services.billing import BillingError
from services.payment_creation import CreationVoided, YooKassaCreationService
from services.payment_reconciliation import PaymentReconciler
from services.yookassa import YooKassaClient

# What YooKassa returns for the QR confirmation of an SBP payment.
SBP_LINK = (
    "https://qr.nspk.ru/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ"
    "?type=02&bank=100000000111&sum=29900&cur=RUB&crc=AB75"
)


class Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lose_first_response = False
        self.omit_confirmation = False
        self.completed = False
        self.sbp_link = SBP_LINK
        self.remote: dict[str, Any] = {}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=self.remote)
        key = request.headers["Idempotence-Key"]
        body = json.loads(request.content)
        self.calls.append((key, body))
        if body["confirmation"]["type"] == "qr":
            confirmation = {"type": "qr", "confirmation_data": self.sbp_link}
        else:
            confirmation = {
                "type": "redirect",
                "confirmation_url": "https://yoomoney.ru/checkout/test",
            }
        self.remote = {
            "id": str(UUID(key)),
            "amount": body["amount"],
            "metadata": body["metadata"],
            "paid": False,
            "status": "pending",
            "confirmation": confirmation,
        }
        if self.omit_confirmation:
            self.remote.pop("confirmation")
        if self.completed:
            self.remote.update(status="succeeded", paid=True)
        if self.lose_first_response and len(self.calls) == 1:
            raise httpx.ReadTimeout("simulated lost response")
        return httpx.Response(200, json=self.remote)


@pytest.fixture
async def ready(services: Any) -> Any:
    provider = Provider()
    client = YooKassaClient(
        "123456",
        "test_key",
        return_url="https://app.test",
        transport=httpx.MockTransport(provider.handle),
    )
    services.yookassa = client
    services.billing.yookassa = client
    await services.db.create_user(910001, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(910001)
    yield services, provider, client
    await client.close()


@pytest.fixture
async def ready_qr(services: Any) -> Any:
    """The shop confirms SBP payments by QR: the bot shows the SBP link itself."""
    provider = Provider()
    client = YooKassaClient(
        "123456",
        "test_key",
        return_url="https://app.test",
        transport=httpx.MockTransport(provider.handle),
        payment_method="sbp",
        confirmation="qr",
    )
    services.yookassa = client
    services.billing.yookassa = client
    await services.db.create_user(910001, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(910001)
    yield services, provider, client
    await client.close()


async def test_qr_confirmation_stores_and_returns_the_sbp_link(ready_qr: Any) -> None:
    services, provider, _ = ready_qr
    result = await services.billing.create(910001, 1, "sbp")
    assert result.data["payment_url"] == SBP_LINK
    assert result.data["provider"] == "yookassa"
    ((_, body),) = provider.calls
    assert body["confirmation"] == {"type": "qr", "return_url": "https://app.test"}
    assert body["payment_method_data"] == {"type": "sbp"}
    creation = await services.db.get_yookassa_creation(result.payment_id)
    assert creation is not None
    assert creation["confirmation_url"] == SBP_LINK
    # Asking again shows the same order and link: no second payment.
    again = await services.billing.create(910001, 1, "sbp")
    assert again.payment_id == result.payment_id
    assert again.data["payment_url"] == SBP_LINK
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "link",
    [
        "sbp://qr.nspk.ru/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ",
        "http://qr.nspk.ru/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ",
        "https://user@qr.nspk.ru/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ",
    ],
)
async def test_a_qr_answer_that_is_not_an_https_link_is_never_shown(
    ready_qr: Any, link: str
) -> None:
    services, provider, _ = ready_qr
    provider.sbp_link = link
    with pytest.raises(BillingError, match="^provider_error$"):
        await services.billing.create(910001, 1, "sbp")
    (order,) = await services.db.payment_history(910001)
    creation = await services.db.get_yookassa_creation(order["payment_id"])
    assert creation is not None
    assert creation["confirmation_url"] is None
    assert creation["state"] == "pending"


async def test_completed_invoice_waits_for_verified_reconciliation(ready: Any) -> None:
    services, provider, client = ready
    provider.omit_confirmation = True
    provider.completed = True
    before = (await services.db.get_user(910001))["expires_at"]
    for _ in range(2):
        with pytest.raises(BillingError, match="^payment_processing$"):
            await services.billing.create(910001, 1, "sbp")
    history = await services.db.payment_history(910001)
    assert len(history) == 1
    assert len(provider.calls) == 1
    assert history[0]["status"] == "pending"
    stored = await services.db.get_payment(history[0]["payment_id"])
    assert stored["provider_id"].startswith("yookassa:")
    assert (await services.db.get_user(910001))["expires_at"] == before
    poller = PaymentReconciler(services.db, services.provisioning, client, None)
    await poller.poll()
    await poller.poll()
    assert (await services.db.get_user(910001))["expires_at"] == before + 30 * 86400
    assert (await services.db.get_payment(history[0]["payment_id"]))["status"] == "paid"


async def test_api_reports_processing_without_another_checkout(ready: Any) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from tests.conftest import make_init_data
    from webhook_server import create_webhook_app

    services, provider, _ = ready
    provider.omit_confirmation = True
    provider.completed = True
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        auth = await client.post(
            "/api/v1/auth/telegram",
            json={
                "initData": make_init_data(
                    services.settings.bot_token.get_secret_value(), 910001
                )
            },
        )
        tokens = await auth.json()
        response = await client.post(
            "/api/v1/payments/create",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={"months": 1, "method": "sbp"},
        )
        assert response.status == 409
        assert await response.json() == {"error": "payment_processing"}


async def test_timeout_reuses_persisted_order_body_and_key(ready: Any) -> None:
    services, provider, _ = ready
    provider.lose_first_response = True
    with pytest.raises(BillingError):
        await services.billing.create(910001, 1, "sbp")
    history = await services.db.payment_history(910001)
    assert len(history) == 1
    payment_id = history[0]["payment_id"]
    assert history[0]["status"] == "pending"
    assert (await services.db.get_payment(payment_id))["provider_id"] is None
    result = await services.billing.create(910001, 1, "sbp")
    assert result.payment_id == payment_id
    assert provider.calls[0] == provider.calls[1]
    assert len(await services.db.payment_history(910001)) == 1
    again = await services.billing.create(910001, 1, "sbp")
    assert again.payment_id == payment_id
    assert len(provider.calls) == 2


async def test_background_recovery_finds_remote_paid_without_callback(
    ready: Any,
) -> None:
    services, provider, client = ready
    provider.lose_first_response = True
    with pytest.raises(BillingError):
        await services.billing.create(910001, 1, "sbp")
    await services.db._execute("UPDATE yookassa_creations SET next_attempt_at=0")
    # New service instances model process-local state being lost.
    poller = PaymentReconciler(services.db, services.provisioning, client, None)
    await poller.poll()
    provider.remote.update(status="succeeded", paid=True)
    before = (await services.db.get_user(910001))["expires_at"]
    await poller.poll()
    after = (await services.db.get_user(910001))["expires_at"]
    assert after - before == 30 * 86400
    await poller.poll()
    assert (await services.db.get_user(910001))["expires_at"] == after


async def test_db_failure_after_remote_creation_reuses_same_request(
    ready: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, provider, _ = ready
    original = services.db.complete_yookassa_creation

    async def fail(*args: Any) -> None:
        raise OSError("simulated commit failure")

    monkeypatch.setattr(services.db, "complete_yookassa_creation", fail)
    with pytest.raises(OSError, match="simulated commit failure"):
        await services.billing.create(910001, 1, "sbp")
    assert len(await services.db.payment_history(910001)) == 1
    monkeypatch.setattr(services.db, "complete_yookassa_creation", original)
    await services.billing.create(910001, 1, "sbp")
    assert provider.calls[0] == provider.calls[1]


async def test_concurrent_invoice_creation_produces_one_order(ready: Any) -> None:
    services, provider, _ = ready
    results = await asyncio.gather(
        services.billing.create(910001, 1, "sbp"),
        services.billing.create(910001, 1, "sbp"),
        return_exceptions=True,
    )
    assert any(not isinstance(result, Exception) for result in results)
    assert len(provider.calls) == 1
    assert len(await services.db.payment_history(910001)) == 1


async def test_expired_window_voids_the_order_and_a_new_one_gets_a_link(
    ready: Any,
) -> None:
    services, provider, _ = ready
    provider.lose_first_response = True
    with pytest.raises(BillingError):
        await services.billing.create(910001, 1, "sbp")
    lost = await services.db.open_payment(910001)
    assert lost is not None
    await services.db._execute("UPDATE yookassa_creations SET retry_until=0")
    created = await services.billing.create(910001, 1, "sbp")
    # The first order never got a link, so nobody could have paid it.
    assert created.payment_id != lost["payment_id"]
    assert created.data["payment_url"]
    assert (await services.db.get_payment(lost["payment_id"]))["status"] == "cancelled"
    assert len(provider.calls) == 2


async def test_store_change_voids_the_old_order_and_never_replays_it(
    ready: Any,
) -> None:
    services, provider, _ = ready
    provider.lose_first_response = True
    with pytest.raises(BillingError):
        await services.billing.create(910001, 1, "sbp")
    job = await services.db.open_yookassa_creation(910001, 1, "basic", "123456")
    assert job is not None
    other = YooKassaClient(
        "999999",
        "test_key",
        return_url="https://app.test",
        transport=httpx.MockTransport(provider.handle),
    )
    try:
        with pytest.raises(CreationVoided):
            await YooKassaCreationService(services.db, other).issue(job)
        assert len(provider.calls) == 1
    finally:
        await other.close()
    assert (await services.db.get_payment(job["payment_id"]))["status"] == "cancelled"
