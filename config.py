"""Application settings.

Secrets are read from Docker Secrets (/run/secrets/<name>) first, then from
environment variables / .env for local development.
"""

from __future__ import annotations

import math
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

from db.endpoint import MIN_TOKEN_LENGTH, validate_gateway_url

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))

# Tariffs: tier -> months -> price. RUB for SBP (integer roubles), USDT as Decimal.
# USDT prices follow the RUB ratio of the basic tier, rounded to 0.50.
# "standard" is the 2026-11 model (1-6 months, two devices, traffic quota).
# It is sold only when ENABLED_TIERS lists it, so an existing deployment keeps
# its current terms until the owner switches deliberately.
TIER_PRICES_RUB: dict[str, dict[int, int]] = {
    "standard": {1: 299, 2: 549, 3: 799, 4: 999, 5: 1199, 6: 1399},
    "basic": {1: 300, 3: 800, 6: 1500, 12: 2700},
    "complete": {1: 450, 3: 1200, 6: 2200, 12: 4000},
    "prime": {1: 700, 3: 1800, 6: 3500, 12: 6000},
}
TIER_PRICES_USDT: dict[str, dict[int, Decimal]] = {
    "standard": {
        1: Decimal("3.50"),
        2: Decimal("6.50"),
        3: Decimal("9.50"),
        4: Decimal("11.50"),
        5: Decimal("14.00"),
        6: Decimal("16.50"),
    },
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
# Tiers the public offer (legal/oferta.md, clause 3) lets us sell. Other tiers stay
# defined only for subscribers who paid for them earlier, until their period ends.
SELLABLE_TIERS: tuple[str, ...] = ("standard",)


GIB = 1024**3
# Marzban's "month" reset strategy restarts the counter every 30 days counted
# from the user's creation or last reset, not on a calendar month.
TRAFFIC_RESET_STRATEGIES = ("no_reset", "day", "week", "month", "year")


@dataclass(frozen=True, slots=True)
class Tier:
    slug: str
    title: str
    # Contractual limit (offer, clause 3.4). Marzban has no per-user IP limit, and the
    # service does not log client IPs, so the limit is not enforced technically.
    devices: int
    priority_support: bool
    features: tuple[str, ...]
    # Shared traffic quota of the whole subscription, enforced by Marzban
    # (data_limit). 0 means unlimited, as in the original tiers.
    traffic_gb: int = 0
    traffic_reset: str = "no_reset"

    @property
    def traffic_bytes(self) -> int:
        return self.traffic_gb * GIB


# Only features that exist today are listed here: they appear in the bot, the apps
# and the offer. Multi-hop, a dedicated IP and bundled cloud storage are not built.
TIERS: dict[str, Tier] = {
    "standard": Tier(
        slug="standard",
        title="Стандарт",
        devices=2,
        priority_support=False,
        features=(
            "Шифрование подключения в публичном Wi-Fi: кафе, отели, аэропорты",
            "2 устройства по одной подписке, например телефон и ноутбук",
            "200 ГБ трафика на каждые 30 дней, общий для всех устройств",
            "Защита от вредоносных и фишинговых сайтов на стороне сервера",
            "Фильтр сайтов 18+ и азартных игр",
            "Протокол VLESS + XTLS-Reality",
        ),
        traffic_gb=200,
        traffic_reset="month",
    ),
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


def all_periods() -> list[int]:
    """Every period (months) sold in at least one tier."""
    return sorted({months for prices in TIER_PRICES_RUB.values() for months in prices})


def stars_price(tier: str, months: int, rub_per_star: Decimal) -> int:
    """Telegram Stars price: the rouble price converted at the owner's rate.

    Rounded up to whole stars, because XTR has no fractional unit.
    """
    value = Decimal(TIER_PRICES_RUB[tier][months]) / rub_per_star
    return max(1, math.ceil(value))


def format_gb(value_bytes: int) -> str:
    gb = value_bytes / GIB
    return f"{gb:.1f}" if gb < 10 else f"{gb:.0f}"


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
    # The database on another server, behind db.gateway: for example
    # http://10.204.0.1:8444 inside the tunnel between the servers. Unset = the
    # local file above. database_path and db_key then belong to the gateway.
    database_url: str | None = None
    # Docker secret: secrets/db_gateway_token.txt (update_bot.sh --database-gateway).
    db_gateway_token: SecretStr | None = None
    log_level: str = "INFO"
    log_dir: str = "logs"
    # Five years, the term in legal/privacy.md, section 7. A backstop: closed
    # months are archived and removed much earlier (scripts/retention_archiver.py).
    conn_log_retention_days: int = 1826
    activity_logging_enabled: bool = False

    # --- Marzban ------------------------------------------------------------
    marzban_url: str = "https://marzban.internal:8000"
    marzban_username: str = "admin"
    marzban_password: SecretStr
    marzban_inbound_tag: str = "VLESS TCP REALITY"
    marzban_verify_tls: bool = True
    marzban_timeout: float = 15.0

    # --- Locations and configuration delivery -------------------------------
    # link: one vless:// link to the first location (original behaviour).
    # subscription: the Marzban subscription URL with every location; clients
    # refresh the server list themselves when servers are added or removed.
    config_delivery: Literal["link", "subscription"] = "link"
    # Public origin of the Marzban subscription endpoint, used when Marzban
    # reports a relative /sub/... path (XRAY_SUBSCRIPTION_URL_PREFIX unset).
    subscription_base_url: str | None = None
    # True once scripts/ru_direct.sh --apply has put the rule "Russian sites
    # directly" into Marzban's subscription: v2rayNG then needs no setup and the
    # bot, the site and the Mini App say so instead of showing steps.
    ru_direct_subscription: bool = False
    # Display name of the server that runs the Marzban core itself.
    main_location_name: str = Field("Основной сервер", min_length=1, max_length=64)

    # --- AmneziaWG: second protocol, one key per device ----------------------
    # Requires the separate cyberdefency-awg container (scripts/install_awg.sh).
    awg_enabled: bool = False
    awg_agent_url: str = "http://cyberdefency-awg:8090"
    awg_agent_token: SecretStr | None = None

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
    # SBP confirmation: redirect = YooMoney checkout page; qr = the bot shows the
    # SBP QR code and a bank-app button itself (update_bot.sh checks it first).
    yookassa_confirmation: Literal["redirect", "qr"] = "redirect"
    # Explicit per-provider routing; never apply a global proxy to the VPN/bot.
    yookassa_egress: Literal["direct", "ru_gateway"] = "direct"
    yookassa_proxy_url: str | None = None
    # Bot API (Telegram) calls through an HTTP CONNECT proxy outside the RU network.
    telegram_proxy_url: str | None = None

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

    # --- Server traffic allowance ---------------------------------------------
    # Monthly traffic of the hosting plan in decimal GB (5 TB = 5000). The bot
    # forecasts the period and warns the owner (services/server_traffic.py);
    # 0 turns this off.
    server_traffic_limit_gb: int = Field(5000, ge=0, le=1_000_000)
    # What the provider counts: in + out ("both", the safe default), "out" or "in".
    server_traffic_counts: Literal["both", "out", "in"] = "both"
    # Day of the month the provider's counter starts again.
    server_traffic_reset_day: int = Field(1, ge=1, le=28)
    # Pause new trials at 90 % of the allowance. Off until clause 1.6 of the
    # offer allows it (docs/SERVER_MAINTENANCE.md has the wording).
    server_traffic_pause_trials: bool = False

    # --- Telegram Stars (in-bot sales of digital services) -------------------
    # Telegram requires Stars (XTR) for digital goods sold inside bots.
    stars_enabled: bool = False
    # How many roubles one star is counted at when converting tariff prices.
    stars_rub_per_star: Decimal = Field(Decimal("1.60"), gt=0, le=100)
    # Monthly auto-renewal through a Telegram Stars subscription (30 days is the
    # only period Telegram supports). Off until the offer says so (clause 3.3.1).
    stars_autorenew_enabled: bool = False
    # YooKassa connected through @BotFather → Payments («ЮKassa: платежи»):
    # ruble invoices paid inside Telegram. Docker secret telegram_payment_token
    # or TELEGRAM_PAYMENT_TOKEN. Takes precedence over the REST checkout in the bot.
    telegram_payment_token: SecretStr | None = None
    # Ruble checkout (YooKassa) inside the bot. See docs/PAYMENTS_TELEGRAM.md.
    yookassa_in_bot: bool = True

    # --- Business -----------------------------------------------------------
    trial_seconds: int = 86400
    # Traffic quota of the free trial in GB; 0 keeps the trial unlimited.
    trial_traffic_gb: int = Field(0, ge=0, le=1000)
    referral_acquisition_enabled: bool = False
    referral_inviter_bonus_days: int = 15
    referral_invitee_bonus_days: int = 3
    support_username: str | None = None
    # Comma-separated tier keys offered for sale; plain str for the same reason as
    # cors_origins_csv. Use the ``enabled_tiers`` property: it keeps only tiers the
    # offer allows (SELLABLE_TIERS), whatever an older .env says.
    enabled_tiers_csv: str = Field("standard", validation_alias="enabled_tiers")

    @field_validator("crypto_pay_token", mode="before")
    @classmethod
    def _blank_token_means_no_token(cls, value: object) -> object:
        # An emptied secret file turns USDT payments off: nothing else is read.
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

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

    @field_validator("subscription_base_url")
    @classmethod
    def _subscription_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.rstrip("/")
        try:
            url = urlsplit(value)
            _ = url.port
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and url.path == ""
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("subscription_base_url must be an HTTPS origin, no path")
        return value

    @field_validator("awg_agent_url")
    @classmethod
    def _awg_agent_origin(cls, value: str) -> str:
        value = value.rstrip("/")
        try:
            url = urlsplit(value)
            _ = url.port
            valid = (
                url.scheme in ("http", "https")
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and url.path == ""
                and not url.query
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("awg_agent_url must be an http(s) origin without a path")
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
        "database_url",
        "db_gateway_token",
        "yookassa_shop_id",
        "yookassa_secret_key",
        "yookassa_return_url",
        "yookassa_proxy_url",
        "telegram_proxy_url",
        "subscription_base_url",
        "awg_agent_token",
        "telegram_payment_token",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        # An empty Docker secret file must disable the feature, not enable it
        # with an empty credential.
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _private_database_gateway(cls, value: str | None) -> str | None:
        return None if value is None else validate_gateway_url(value)

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

    @field_validator("telegram_proxy_url")
    @classmethod
    def _plain_telegram_proxy(cls, value: str | None) -> str | None:
        """Bot API egress through a proxy: plain http to a literal IPv4 address."""
        if value is None:
            return None
        try:
            url = urlsplit(value)
            address = ip_address(url.hostname or "")
            valid = (
                url.scheme == "http"
                and isinstance(address, IPv4Address)
                and url.port is not None
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
                "telegram_proxy_url must be http://IPv4:port without credentials, "
                "path, query or fragment"
            )
        return f"http://{address}:{url.port}"

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
        if self.database_url and (
            self.db_gateway_token is None
            or len(self.db_gateway_token.get_secret_value()) < MIN_TOKEN_LENGTH
        ):
            problems.append(
                f"db_gateway_token ({MIN_TOKEN_LENGTH}+ characters) is required "
                "with database_url"
            )
        if self.vapid_private and not self.vapid_public:
            problems.append("vapid_public is required when vapid_private is set")
        keys = [
            k.strip().lower() for k in self.enabled_tiers_csv.split(",") if k.strip()
        ]
        if not keys or any(key not in TIERS for key in keys):
            problems.append(f"enabled_tiers must list tiers from: {', '.join(TIERS)}")
        if self.telegram_payment_token is not None and not re.fullmatch(
            r"[0-9]{3,20}:[A-Za-z0-9_:-]{2,256}",
            self.telegram_payment_token.get_secret_value(),
        ):
            problems.append(
                "telegram_payment_token does not look like a BotFather payment token"
            )
        if self.awg_enabled and (
            self.awg_agent_token is None
            or len(self.awg_agent_token.get_secret_value()) < 32
        ):
            problems.append(
                "awg_agent_token (32+ characters) is required for AmneziaWG"
            )
        if self.bot_mode == "webhook" and not self.telegram_webhook_secret:
            problems.append("telegram_webhook_secret is required in webhook mode")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def disabled_features(self) -> list[str]:
        features = {
            "yookassa_telegram (telegram_payment_token)": self.telegram_payment_token,
            "usdt_payments (crypto_pay_token)": self.crypto_pay_token,
            "web_push (vapid_private)": self.vapid_private,
            "abuse_reports (abuse_report_token)": self.abuse_report_token,
            # With database_url the key belongs to the gateway, not to the bot.
            "database_encryption (db_key)": self.db_key or self.database_url,
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
        sellable = [item for item in items if item in SELLABLE_TIERS]
        return sellable or list(SELLABLE_TIERS)

    @property
    def default_tier(self) -> str:
        """The tier used for deep links without a tier and for the trial."""
        tiers = self.enabled_tiers
        return tiers[0] if tiers else DEFAULT_TIER

    @property
    def payment_methods_in_bot(self) -> list[str]:
        """Ordinary payment menu of the bot; USDT has its own entry point."""
        methods = []
        if self.stars_enabled:
            methods.append("stars")
        if self.yookassa_in_bot:
            methods.append("sbp")
        return methods

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
    def privacy_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/privacy"

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
