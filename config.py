"""Application settings.

Secrets are read from Docker Secrets (/run/secrets/<name>) first, then from
environment variables / .env for local development.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from ipaddress import IPv4Address, IPv4Network, ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))

# Tariffs: tier -> months -> price. RUB for SBP (integer roubles), USDT as Decimal.
# USDT prices follow the RUB ratio of the basic tier, rounded to 0.50.
TIER_PRICES_RUB: dict[str, dict[int, int]] = {
    "basic": {1: 300, 3: 800, 6: 1500, 12: 2700},
    "complete": {1: 450, 3: 1200, 6: 2200, 12: 4000},
    "prime": {1: 700, 3: 1800, 6: 3500, 12: 6000},
}
TIER_PRICES_USDT: dict[str, dict[int, Decimal]] = {
    "basic": {
        1: Decimal("3.50"),
        3: Decimal("9.00"),
        6: Decimal("16.50"),
        12: Decimal("29.00"),
    },
    "complete": {
        1: Decimal("5.50"),
        3: Decimal("13.50"),
        6: Decimal("24.00"),
        12: Decimal("43.00"),
    },
    "prime": {
        1: Decimal("8.00"),
        3: Decimal("20.50"),
        6: Decimal("38.50"),
        12: Decimal("64.50"),
    },
}
DEFAULT_TIER = "basic"


@dataclass(frozen=True, slots=True)
class Tier:
    slug: str
    title: str
    # Contractual limit (offer, clause 3.4). Marzban has no per-user IP limit, and the
    # service does not log client IPs, so the limit is not enforced technically.
    devices: int
    priority_support: bool
    features: tuple[str, ...]


# Only features that exist today are listed here: they appear in the bot, the apps
# and the offer. Multi-hop, a dedicated IP and bundled cloud storage are not built.
TIERS: dict[str, Tier] = {
    "basic": Tier(
        slug="basic",
        title="Basic",
        devices=5,
        priority_support=False,
        features=(
            "Протокол VLESS + XTLS-Reality (flow xtls-rprx-vision)",
            "До 5 устройств",
            "Блокировка вредоносных и фишинговых ресурсов, ресурсов 18+ и азартных игр",
        ),
    ),
    "complete": Tier(
        slug="complete",
        title="Complete",
        devices=10,
        priority_support=True,
        features=(
            "Протокол VLESS + XTLS-Reality (flow xtls-rprx-vision)",
            "До 10 устройств",
            "Блокировка вредоносных и фишинговых ресурсов, ресурсов 18+ и азартных игр",
            "Приоритетная поддержка",
        ),
    ),
    "prime": Tier(
        slug="prime",
        title="Prime",
        devices=15,
        priority_support=True,
        features=(
            "Протокол VLESS + XTLS-Reality (flow xtls-rprx-vision)",
            "До 15 устройств",
            "Блокировка вредоносных и фишинговых ресурсов, ресурсов 18+ и азартных игр",
            "Приоритетная поддержка",
        ),
    ),
}


def savings_percent(tier: str, months: int) -> int:
    """Discount of a long period against paying month by month, in whole percent."""
    prices = TIER_PRICES_RUB[tier]
    full = prices[1] * months
    return round((full - prices[months]) * 100 / full) if months > 1 else 0


def device_note(tier: str) -> str:
    """Panel-side note: the tier and its device limit, visible to the admin."""
    return f"tier={tier} devices={TIERS[tier].devices}"


DAYS_PER_MONTH = 30
DB_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Unset values in .env.example (KEY=) mean "not configured", not "".
        env_ignore_empty=True,
        secrets_dir=str(SECRETS_DIR) if SECRETS_DIR.is_dir() else None,
    )

    # --- Telegram -----------------------------------------------------------
    bot_token: SecretStr
    bot_username: str = Field(pattern=r"^[A-Za-z0-9_]{5,32}$")
    admin_telegram_id: int
    # Telegram identifies an account, not a specific phone/PC session.
    # Keep management operations outside Telegram for the owner-only PC model.
    admin_telegram_read_only: bool = True
    bot_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_secret: SecretStr | None = None
    # Domain registered via @BotFather /setdomain for the Login Widget.
    telegram_bot_domain: str = "node.example.com"

    # --- HTTP ---------------------------------------------------------------
    # Required because the Telegram Mini App needs the real, public HTTPS URL.
    public_base_url: str
    http_host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    http_port: int = 8080
    # Comma-separated in CORS_ORIGINS; plain str so pydantic-settings does not
    # expect JSON. Use the ``cors_origins`` property.
    cors_origins_csv: str = Field("", validation_alias="cors_origins")
    trust_forwarded_for: bool = True
    metrics_token: SecretStr | None = None

    # --- Storage / logging ------------------------------------------------
    database_path: str = "data/bot.db"
    # SQLCipher key: 64 hex chars (Docker secret db_key). Unset = plain SQLite.
    db_key: SecretStr | None = None
    log_level: str = "INFO"
    log_dir: str = "logs"
    conn_log_retention_days: int = 180
    activity_logging_enabled: bool = False

    # --- Marzban ------------------------------------------------------------
    marzban_url: str = "https://marzban.internal:8000"
    marzban_username: str = "admin"
    marzban_password: SecretStr
    marzban_inbound_tag: str = "VLESS TCP REALITY"
    marzban_verify_tls: bool = True
    marzban_timeout: float = 15.0

    # --- Crypto Pay (@CryptoBot), outside Russia only -----------------------
    crypto_pay_token: SecretStr | None = None
    crypto_pay_testnet: bool = False

    # --- SBP (Russia only) --------------------------------------------------
    sbp_provider: Literal["manual", "robokassa", "payanyway", "yookassa"] = "manual"
    sbp_qr_url: str = "https://qr.nspk.ru/REPLACE_WITH_YOUR_QR"
    sbp_payment_url: str = "https://qr.nspk.ru/REPLACE_WITH_YOUR_QR"
    sbp_provider_token: SecretStr | None = None

    # --- YooKassa (sbp_provider=yookassa) -----------------------------------
    yookassa_shop_id: str | None = None
    yookassa_secret_key: SecretStr | None = None
    # self_employed: НПД, чеки в «Мой налог» формирует ЮKassa.
    # fiscal: ИП/ООО, чек 54-ФЗ с email покупателя (коды ниже).
    yookassa_receipt_mode: Literal["self_employed", "fiscal"] = "self_employed"
    yookassa_vat_code: int = Field(1, ge=1, le=12)  # 1 = без НДС
    yookassa_tax_system_code: int | None = Field(2, ge=1, le=6)  # 2 = УСН доходы
    yookassa_return_url: str | None = None
    # Explicit per-provider routing; never apply a global proxy to the VPN/bot.
    yookassa_egress: Literal["direct", "ru_gateway"] = "direct"
    yookassa_proxy_url: str | None = None

    # --- Auth / API ---------------------------------------------------------
    jwt_secret: SecretStr
    jwt_access_ttl_seconds: int = 15 * 60
    jwt_refresh_ttl_seconds: int = 30 * 24 * 3600
    telegram_auth_max_age_seconds: int = 300

    # --- Web Push -----------------------------------------------------------
    vapid_public: str | None = None
    vapid_private: SecretStr | None = None
    vapid_contact: str = "mailto:admin@node.example.com"

    # --- Abuse / anomalies --------------------------------------------------
    abuse_report_token: SecretStr | None = None
    autoban_mode: Literal["review", "automatic"] = "review"
    traffic_anomaly_gb_per_day: int = 800
    autoban_gb_per_day: int = 2000

    # --- Business -----------------------------------------------------------
    trial_seconds: int = 86400
    referral_acquisition_enabled: bool = False
    referral_inviter_bonus_days: int = 15
    referral_invitee_bonus_days: int = 3
    support_username: str | None = None
    # Comma-separated tier keys offered for sale (e.g. "basic,complete"); plain str
    # for the same reason as cors_origins_csv. Use the ``enabled_tiers`` property.
    enabled_tiers_csv: str = Field(
        "basic,complete,prime", validation_alias="enabled_tiers"
    )

    @field_validator("public_base_url")
    @classmethod
    def _public_base_url_is_real_https(cls, value: str) -> str:
        value = value.rstrip("/")
        try:
            url = urlsplit(value)
            hostname = (url.hostname or "").lower()
            _ = url.port
            invalid_host = (
                not hostname
                or hostname == "example.com"
                or hostname.endswith((".example.com", ".invalid", ".localhost"))
            )
            valid = (
                url.scheme == "https"
                and not invalid_host
                and url.username is None
                and url.password is None
                and url.path in ("", "/")
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "public_base_url must be a real public HTTPS origin without a path"
            )
        return value

    @field_validator("marzban_url")
    @classmethod
    def _strip_marzban_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator(
        "telegram_webhook_secret",
        "metrics_token",
        "crypto_pay_token",
        "sbp_provider_token",
        "vapid_public",
        "vapid_private",
        "abuse_report_token",
        "support_username",
        "db_key",
        "yookassa_shop_id",
        "yookassa_secret_key",
        "yookassa_return_url",
        "yookassa_proxy_url",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        # An empty Docker secret file must disable the feature, not enable it
        # with an empty credential.
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("yookassa_proxy_url")
    @classmethod
    def _private_payment_proxy(cls, value: str | None) -> str | None:
        """The plaintext CONNECT hop must be inside the private server tunnel."""
        if value is None:
            return None
        try:
            url = urlsplit(value)
            address = ip_address(url.hostname or "")
            networks = (
                IPv4Network("10.0.0.0/8"),
                IPv4Network("172.16.0.0/12"),
                IPv4Network("192.168.0.0/16"),
            )
            valid = (
                url.scheme == "http"
                and isinstance(address, IPv4Address)
                and any(address in subnet for subnet in networks)
                and url.port is not None
                and 1024 <= url.port <= 65535
                and url.username is None
                and url.password is None
                and url.path in ("", "/")
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "yookassa_proxy_url must use http, an RFC1918 IPv4 and explicit "
                "unprivileged port; credentials, query and path are not allowed"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_secrets(self) -> Settings:
        """Fails at startup instead of silently running with weak or missing secrets.

        Error messages name the field only, never the value.
        """
        problems: list[str] = []
        if len(self.jwt_secret.get_secret_value()) < 64:
            problems.append("jwt_secret must be at least 64 characters (32 bytes hex)")
        if not self.bot_token.get_secret_value().strip():
            problems.append("bot_token is empty")
        if not self.marzban_password.get_secret_value().strip():
            problems.append("marzban_password is empty")
        if (
            self.sbp_provider in ("robokassa", "payanyway")
            and not self.sbp_provider_token
        ):
            problems.append("sbp_provider_token is required for an SBP aggregator")
        if self.sbp_provider == "yookassa" and not (
            self.yookassa_shop_id and self.yookassa_secret_key
        ):
            problems.append(
                "yookassa_shop_id and yookassa_secret_key are required for YooKassa"
            )
        if self.yookassa_egress == "ru_gateway" and not self.yookassa_proxy_url:
            problems.append("yookassa_proxy_url is required for ru_gateway")
        if self.yookassa_egress == "direct" and self.yookassa_proxy_url:
            problems.append("yookassa_proxy_url requires yookassa_egress=ru_gateway")
        if self.db_key and not DB_KEY_RE.fullmatch(self.db_key.get_secret_value()):
            problems.append("db_key must be 64 hex characters")
        if self.vapid_private and not self.vapid_public:
            problems.append("vapid_public is required when vapid_private is set")
        keys = self.enabled_tiers
        if not keys or any(key not in TIERS for key in keys):
            problems.append(f"enabled_tiers must list tiers from: {', '.join(TIERS)}")
        if self.bot_mode == "webhook" and not self.telegram_webhook_secret:
            problems.append("telegram_webhook_secret is required in webhook mode")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def disabled_features(self) -> list[str]:
        features = {
            "usdt_payments (crypto_pay_token)": self.crypto_pay_token,
            "web_push (vapid_private)": self.vapid_private,
            "abuse_reports (abuse_report_token)": self.abuse_report_token,
            "database_encryption (db_key)": self.db_key,
        }
        return [name for name, value in features.items() if value is None]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Docker Secrets win over env so a stale env var never shadows a secret.
        return (init_settings, file_secret_settings, env_settings, dotenv_settings)

    @property
    def cors_origins(self) -> list[str]:
        items = (item.strip() for item in self.cors_origins_csv.split(","))
        return [item for item in items if item]

    @property
    def enabled_tiers(self) -> list[str]:
        items = (item.strip().lower() for item in self.enabled_tiers_csv.split(","))
        return [item for item in items if item]

    @property
    def referral_link_base(self) -> str:
        return f"https://t.me/{self.bot_username}?start="

    @property
    def tma_url(self) -> str:
        return f"{self.public_base_url}/tma/"

    @property
    def offer_url(self) -> str:
        # The public offer must open before authentication, including from a
        # Telegram message. The SPA's /#/legal route is behind the login gate.
        return f"{self.public_base_url.rstrip('/')}/oferta"

    @property
    def yookassa_return(self) -> str:
        return self.yookassa_return_url or f"https://t.me/{self.bot_username}"

    @property
    def crypto_pay_base_url(self) -> str:
        host = "testnet-pay.crypt.bot" if self.crypto_pay_testnet else "pay.crypt.bot"
        return f"https://{host}/api"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
