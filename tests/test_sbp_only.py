"""Ruble payments through YooKassa are SBP only: no card form is offered."""

from __future__ import annotations

import json

import httpx
import pytest

from services.yookassa import YooKassaClient, confirmation_link, is_sbp_link


def client(**extra: object) -> YooKassaClient:
    return YooKassaClient(
        "123456", "test_" + "x" * 40, return_url="https://t.me/b", **extra
    )  # type: ignore[arg-type]


def test_sbp_payment_method_is_sent_for_self_employed() -> None:
    body = client(payment_method="sbp").build_payment(
        payment_id="a" * 32, amount_rub=300, months=1, email=None
    )
    assert body["payment_method_data"] == {"type": "sbp"}
    assert body["confirmation"]["type"] == "redirect"


def test_sbp_payment_method_is_sent_for_fiscal_too() -> None:
    body = client(payment_method="sbp", receipt_mode="fiscal").build_payment(
        payment_id="b" * 32,
        amount_rub=300,
        months=1,
        email="buyer@example.com",
    )
    assert body["payment_method_data"] == {"type": "sbp"}
    assert "receipt" in body


def test_without_setting_the_body_is_unchanged() -> None:
    body = client().build_payment(
        payment_id="c" * 32, amount_rub=300, months=1, email=None
    )
    assert "payment_method_data" not in body
    json.dumps(body)  # still serialisable


def test_only_sbp_or_none_is_accepted() -> None:
    with pytest.raises(ValueError, match="payment_method"):
        client(payment_method="bank_card")


SBP_LINK = (
    "https://qr.nspk.ru/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ"
    "?type=02&bank=100000000111&sum=29900&cur=RUB&crc=AB75"
)


def test_qr_confirmation_asks_for_the_sbp_link() -> None:
    """QR: YooKassa answers with the SBP link; no YooMoney page is involved."""
    body = client(payment_method="sbp", confirmation="qr").build_payment(
        payment_id="d" * 32, amount_rub=299, months=1, email=None
    )
    assert body["payment_method_data"] == {"type": "sbp"}
    # The bank app opens return_url after the payment (SBP only, per the API).
    assert body["confirmation"] == {"type": "qr", "return_url": "https://t.me/b"}


def test_qr_is_for_sbp_only_and_other_scenarios_are_refused() -> None:
    with pytest.raises(ValueError, match="qr confirmation needs payment_method sbp"):
        client(confirmation="qr")
    with pytest.raises(ValueError, match="confirmation must be redirect or qr"):
        client(payment_method="sbp", confirmation="embedded")


def test_the_link_is_read_from_either_confirmation() -> None:
    assert confirmation_link({"type": "qr", "confirmation_data": SBP_LINK}) == SBP_LINK
    page = "https://yoomoney.ru/checkout/x"
    assert confirmation_link({"type": "redirect", "confirmation_url": page}) == page
    # A QR answer is never mistaken for a page link, and the other way round.
    assert confirmation_link({"type": "qr", "confirmation_url": page}) is None
    redirect_with_data = {"type": "redirect", "confirmation_data": SBP_LINK}
    assert confirmation_link(redirect_with_data) is None
    assert confirmation_link(None) is None


async def test_create_payment_returns_the_sbp_link() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["confirmation"]["type"] == "qr"
        return httpx.Response(
            200,
            json={
                "id": "2c1b2a3d-000f-5000-8000-1a2b3c4d5e6f",
                "status": "pending",
                "confirmation": {"type": "qr", "confirmation_data": SBP_LINK},
            },
        )

    yookassa = client(
        payment_method="sbp",
        confirmation="qr",
        transport=httpx.MockTransport(answer),
    )
    try:
        created = await yookassa.create_payment(
            payment_id="e" * 32, amount_rub=299, months=1, email=None
        )
    finally:
        await yookassa.close()
    assert created.confirmation_url == SBP_LINK


@pytest.mark.parametrize(
    ("link", "shown"),
    [
        (SBP_LINK, True),
        ("https://QR.NSPK.RU/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ", True),
        ("https://qr.nspk.ru:443/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ", True),
        ("http://qr.nspk.ru/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ", False),
        ("https://qr.nspk.ru/", False),
        ("https://qr.nspk.ru:8443/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ", False),
        ("https://user@qr.nspk.ru/AS1A005FUPOCRTFF8M7OSAQRJ7I5R0HQ", False),
        ("https://qr.nspk.ru.example.com/AS1A", False),
        ("https://example.com/qr.nspk.ru/AS1A", False),
        ("https://yoomoney.ru/checkout/x", False),
        ("https://qr.nspk.ru/AS1A 005", False),
        ("https://qr.nspk.ru/AS1A\n005", False),
        ("https://qr.nspk.ru:bad/AS1A", False),
        ("javascript:alert(1)", False),
        ("https://qr.nspk.ru/" + "A" * 4100, False),
    ],
)
def test_only_an_https_nspk_link_counts_as_an_sbp_link(link: str, shown: bool) -> None:
    assert is_sbp_link(link) is shown
