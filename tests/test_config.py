from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import main
from config import Settings, get_settings

SHORT_SECRET = "short-but-secret-value"


def test_missing_required_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "jwt_secret" in str(exc.value)


def test_weak_secret_fails_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", SHORT_SECRET)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]
    errors = exc.value.errors(include_input=False)
    assert "64 characters" in errors[0]["msg"]
    assert SHORT_SECRET not in errors[0]["msg"]


def test_aggregator_requires_provider_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBP_PROVIDER", "robokassa")
    monkeypatch.delenv("SBP_PROVIDER_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="sbp_provider_token"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_load_settings_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("JWT_SECRET", SHORT_SECRET)
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as exc:
        main.load_settings()
    get_settings.cache_clear()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert SHORT_SECRET not in err
    assert "Traceback" not in err


def test_optional_features_reported(settings: Settings) -> None:
    assert "web_push (vapid_private)" in settings.disabled_features()


def test_public_base_url_rejects_example_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://node.example.com")
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_public_base_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_offer_url_is_public_and_does_not_require_spa_login(settings: Settings) -> None:
    assert settings.offer_url == f"{settings.public_base_url}/oferta"


def test_env_example_with_docker_secrets_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The compose path: env_file from .env.example, secrets as files."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    for line in example.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            monkeypatch.setenv(key, value)
    secrets = {
        "bot_token": "123456:" + "x" * 35,
        "hiddify_api_key": "p" * 16,
        "jwt_secret": "a" * 64,
        "crypto_pay_token": "",
        "vapid_private": "\n",
        "yookassa_secret_key": "test_secret_value_for_tests",
        "db_key": "ab" * 32,
    }
    for name, value in secrets.items():
        (tmp_path / name).write_text(value, encoding="utf-8")

    monkeypatch.setenv("YOOKASSA_SHOP_ID", "123456")
    settings = Settings(_env_file=None, _secrets_dir=tmp_path)  # type: ignore[call-arg]

    assert settings.cors_origins == []
    assert settings.crypto_pay_token is None
    assert settings.vapid_private is None
    assert settings.metrics_token is None
    assert "usdt_payments (crypto_pay_token)" in settings.disabled_features()
    assert settings.sbp_provider == "yookassa"
    assert settings.public_base_url == "https://cyberdefency.com"
    assert settings.db_key is not None


def test_cors_origins_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


def test_yookassa_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBP_PROVIDER", "yookassa")
    monkeypatch.delenv("YOOKASSA_SHOP_ID", raising=False)
    with pytest.raises(ValidationError, match="yookassa_shop_id"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "123456")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "test_secret_value_for_tests")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.yookassa_return == "https://t.me/test_node_bot"


def test_db_key_must_be_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_KEY", "not-hex")
    with pytest.raises(ValidationError, match="db_key"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("DB_KEY", "ab" * 32)
    assert (
        "database_encryption (db_key)"
        not in Settings(
            _env_file=None  # type: ignore[call-arg]
        ).disabled_features()
    )


def test_unreadable_secret_file_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret = tmp_path / "bot_token"
    secret.write_text("123456:" + "y" * 35, encoding="utf-8")
    secret.chmod(0o000)
    readable = os.access(secret, os.R_OK)  # root can read anyway

    def fake_settings() -> Settings:
        return Settings(_env_file=None, _secrets_dir=tmp_path)  # type: ignore[call-arg]

    monkeypatch.setattr(main, "get_settings", fake_settings)
    if readable:
        from pydantic_settings import SettingsError

        def raising() -> Settings:
            raise SettingsError('error getting value for field "bot_token"')

        monkeypatch.setattr(main, "get_settings", raising)
    with pytest.raises(SystemExit) as exc:
        main.load_settings()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "bot_token" in err
    assert "chown 1000:1000" in err
    assert "Traceback" not in err


def test_brand_animation_is_on_by_default(settings: Settings) -> None:
    assert settings.brand_animation_enabled is True
    example = Path(__file__).resolve().parent.parent / ".env.example"
    assert "BRAND_ANIMATION_ENABLED=true" in example.read_text(encoding="utf-8")
