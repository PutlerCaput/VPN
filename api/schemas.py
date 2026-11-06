"""Request/response schemas for the REST API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=4096)


class RefreshIn(StrictModel):
    refresh_token: str = Field(min_length=20, max_length=2048)


class LogoutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    refresh_token: str | None = Field(default=None, max_length=2048)


class PaymentCreateIn(StrictModel):
    months: Literal[1, 3, 6, 12]
    method: Literal["sbp", "usdt"]
    tier: Literal["basic", "complete", "prime"] = "basic"
    # Receipt email (54-ФЗ) for YooKassa; saved to the profile when given.
    email: str | None = Field(default=None, max_length=254)


class PushKeys(StrictModel):
    p256dh: str = Field(min_length=16, max_length=256)
    auth: str = Field(min_length=8, max_length=64)


class PushSubscribeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    endpoint: HttpUrl
    keys: PushKeys


class PushUnsubscribeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    endpoint: HttpUrl | None = None


class AbuseReportIn(StrictModel):
    node_username: str = Field(pattern=r"^u\d{5,20}$")
    kinds: list[str] = Field(min_length=1, max_length=10)
    source: str = Field(default="ids", max_length=64)
    detail: str | None = Field(default=None, max_length=1000)
