"""SBP without YooKassa (Russia only, RUB); YooKassa is in services/yookassa.py.

Variant A ("manual"): a static business SBP QR from the bank (Sovcombank). The
user pays, sends a screenshot to support, the admin confirms with
/confirm <payment_id> and issues a 54-ФЗ receipt through the online cash register
the same day. Use it only if you have such a register.

Variant B (aggregator): the aggregator POSTs to /sbp/webhook. The body must be
signed with HMAC-SHA256(sbp_provider_token, raw_body), hex, in the
X-Signature header, and contain {"payment_id", "status", "amount", "provider_id"}.
Use only an aggregator that issues 54-ФЗ receipts on your behalf. Aggregators with
their own notification formats need a small adapter that re-signs into this
contract (see README, stage 7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from config import DEFAULT_TIER, TIER_PRICES_RUB
from services.security import is_valid_payment_id, verify_hmac_sha256

SBP_SIGNATURE_HEADER = "X-Signature"


@dataclass(frozen=True, slots=True)
class SbpInstructions:
    qr_url: str
    payment_url: str
    amount_rub: int


@dataclass(frozen=True, slots=True)
class SbpNotification:
    payment_id: str
    amount_rub: int
    provider_id: str


class SbpError(Exception):
    pass


class SbpService:
    def __init__(
        self,
        provider: str,
        qr_url: str,
        payment_url: str,
        provider_token: str | None,
    ) -> None:
        self.provider = provider
        self.qr_url = qr_url
        self.payment_url = payment_url
        self._provider_token = provider_token

    @property
    def is_manual(self) -> bool:
        return self.provider == "manual"

    def instructions(self, months: int, tier: str = DEFAULT_TIER) -> SbpInstructions:
        if months not in TIER_PRICES_RUB.get(tier, {}):
            raise SbpError("unknown tariff")
        return SbpInstructions(
            qr_url=self.qr_url,
            payment_url=self.payment_url,
            amount_rub=TIER_PRICES_RUB[tier][months],
        )

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        if self.is_manual or not self._provider_token:
            return False
        return verify_hmac_sha256(self._provider_token.encode(), raw_body, signature)

    @staticmethod
    def parse_notification(raw_body: bytes) -> SbpNotification | None:
        try:
            data: Any = json.loads(raw_body)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("status") != "paid":
            return None
        payment_id = data.get("payment_id")
        amount = data.get("amount")
        provider_id = data.get("provider_id")
        if not isinstance(payment_id, str) or not is_valid_payment_id(payment_id):
            return None
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return None
        if not isinstance(provider_id, str) or not 0 < len(provider_id) <= 128:
            return None
        return SbpNotification(
            payment_id=payment_id, amount_rub=amount, provider_id=provider_id
        )
