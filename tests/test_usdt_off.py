"""A blank Crypto Pay secret means USDT is off: the emptied file is the switch."""

from __future__ import annotations

from pydantic import SecretStr

from config import Settings


def _with(settings: Settings, token: object) -> Settings:
    return Settings(**{**settings.model_dump(), "crypto_pay_token": token})  # type: ignore[arg-type]


def test_blank_secret_file_turns_usdt_off(settings: Settings) -> None:
    assert _with(settings, "").crypto_pay_token is None
    assert _with(settings, "   \n").crypto_pay_token is None
    assert _with(settings, SecretStr("")).crypto_pay_token is None


def test_real_token_is_kept(settings: Settings) -> None:
    kept = _with(settings, "12345:TESTCRYPTOPAYTOKEN")
    assert kept.crypto_pay_token is not None
    assert kept.crypto_pay_token.get_secret_value() == "12345:TESTCRYPTOPAYTOKEN"
