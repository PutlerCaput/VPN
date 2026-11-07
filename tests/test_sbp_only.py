"""Ruble payments through YooKassa are SBP only: no card form is offered."""

from __future__ import annotations

import json

import pytest

from services.yookassa import YooKassaClient


def client(**extra: object) -> YooKassaClient:
    return YooKassaClient(
        "123456", "test_" + "x" * 40, return_url="https://t.me/b", **extra
    )  # type: ignore[arg-type]


def test_sbp_payment_method_is_sent_for_self_employed() -> None:
    body = client(payment_method="sbp").build_payment(
        payment_id="a" * 32, amount_rub=300, months=1, email=None, tier_title="Стандарт"
    )
    assert body["payment_method_data"] == {"type": "sbp"}
    assert body["confirmation"]["type"] == "redirect"


def test_sbp_payment_method_is_sent_for_fiscal_too() -> None:
    body = client(payment_method="sbp", receipt_mode="fiscal").build_payment(
        payment_id="b" * 32,
        amount_rub=300,
        months=1,
        email="buyer@example.com",
        tier_title="Стандарт",
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
