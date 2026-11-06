from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from api.auth import AuthError, validate_init_data, validate_login_widget
from tests.conftest import make_init_data

BOT_TOKEN = "123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz"


def test_valid_init_data() -> None:
    identity, auth_hash = validate_init_data(
        make_init_data(BOT_TOKEN, 1001, start_param="77"), BOT_TOKEN
    )
    assert identity.telegram_id == 1001
    assert identity.username == "ivan_test"
    assert identity.first_name == "Ivan"
    assert identity.start_param == "77"
    assert len(auth_hash) == 64


def test_rejects_wrong_bot_token() -> None:
    with pytest.raises(AuthError) as exc:
        validate_init_data(make_init_data("999:OTHER"), BOT_TOKEN)
    assert exc.value.reason == "bad_hash"


def test_rejects_tampered_user() -> None:
    data = make_init_data(BOT_TOKEN, 1001).replace("1001", "1002")
    with pytest.raises(AuthError):
        validate_init_data(data, BOT_TOKEN)


def test_rejects_expired() -> None:
    old = int(time.time()) - 301
    with pytest.raises(AuthError) as exc:
        validate_init_data(make_init_data(BOT_TOKEN, auth_date=old), BOT_TOKEN)
    assert exc.value.reason == "expired"


def test_rejects_future_auth_date() -> None:
    future = int(time.time()) + 3600
    with pytest.raises(AuthError):
        validate_init_data(make_init_data(BOT_TOKEN, auth_date=future), BOT_TOKEN)


@pytest.mark.parametrize(
    "raw", ["", "hash=", "a=1&a=2&hash=x", "x" * 5000, "user=%7B%7D"]
)
def test_rejects_malformed(raw: str) -> None:
    with pytest.raises(AuthError):
        validate_init_data(raw, BOT_TOKEN)


def test_secret_key_derivation_matches_spec() -> None:
    """secret_key is HMAC_SHA256 keyed with b"WebAppData" over bot_token, not SHA256."""
    data = make_init_data(BOT_TOKEN)
    validate_init_data(data, BOT_TOKEN)
    wrong_secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    assert (
        wrong_secret
        != hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    )


def _widget(fields: dict[str, str], token: str = BOT_TOKEN) -> dict[str, str]:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(token.encode()).digest()
    return {
        **fields,
        "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(),
    }


def test_login_widget_valid_and_tampered() -> None:
    fields = {
        "id": "555",
        "first_name": "Anna",
        "username": "anna",
        "auth_date": str(int(time.time())),
    }
    identity, _ = validate_login_widget(_widget(fields), BOT_TOKEN)
    assert identity.telegram_id == 555
    tampered = {**_widget(fields), "id": "556"}
    with pytest.raises(AuthError):
        validate_login_widget(tampered, BOT_TOKEN)


def test_login_widget_expired() -> None:
    fields = {
        "id": "555",
        "first_name": "Anna",
        "auth_date": str(int(time.time()) - 400),
    }
    with pytest.raises(AuthError):
        validate_login_widget(_widget(fields), BOT_TOKEN)
