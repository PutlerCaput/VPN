"""Authentication: Telegram Login Widget (PWA), Mini App initData (TMA), JWT.

Every failure surfaces to the client as a uniform 401 {"error": "unauthorized"};
the specific reason is logged (without credentials) for operators only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

import jwt

from db.database import Database

logger = logging.getLogger("api.auth")

JWT_ALGORITHM = "HS256"
REPLAY_TTL_SECONDS = 300
REPLAY_MAX_ENTRIES = 10_000
CLOCK_SKEW_SECONDS = 30


class AuthError(Exception):
    """Raised with an internal reason; never shown to the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    telegram_id: int
    username: str | None
    first_name: str | None
    start_param: str | None = None


class ReplayGuard:
    """Bounded, process-local TTL set of seen auth hashes.

    Deliberately not persisted: a restart starts a fresh replay window.
    """

    def __init__(
        self, ttl: float = REPLAY_TTL_SECONDS, max_entries: int = REPLAY_MAX_ENTRIES
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._task: asyncio.Task[None] | None = None

    def check_and_add(self, value: str, now: float | None = None) -> bool:
        """True if the value is new (and records it); False on replay."""
        current = time.monotonic() if now is None else now
        seen_at = self._seen.get(value)
        if seen_at is not None and current - seen_at < self.ttl:
            return False
        self._seen[value] = current
        self._seen.move_to_end(value)
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return True

    def cleanup(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        while self._seen:
            _key, seen_at = next(iter(self._seen.items()))
            if current - seen_at < self.ttl:
                break
            self._seen.popitem(last=False)

    def __len__(self) -> int:
        return len(self._seen)

    async def _loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self.cleanup()

    def start(self, interval: float = 60.0) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop(interval), name="replay-guard-cleanup"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def _check_auth_date(raw: str | None, max_age: int, now: float | None = None) -> None:
    if raw is None or not raw.isdigit():
        raise AuthError("parse")
    current = time.time() if now is None else now
    auth_date = int(raw)
    if auth_date > current + CLOCK_SKEW_SECONDS:
        raise AuthError("expired")
    if current - auth_date > max_age:
        raise AuthError("expired")


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = 300,
    now: float | None = None,
) -> tuple[TelegramIdentity, str]:
    """Validates Telegram Mini App initData. Returns (identity, hash)."""
    if not init_data or len(init_data) > 4096:
        raise AuthError("parse")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise AuthError("parse") from exc
    params: dict[str, str] = {}
    for key, value in pairs:
        if key in params:
            raise AuthError("parse")
        params[key] = value
    received_hash = params.pop("hash", None)
    if not received_hash:
        raise AuthError("parse")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise AuthError("bad_hash")
    _check_auth_date(params.get("auth_date"), max_age, now)
    try:
        user = json.loads(params.get("user", ""))
        telegram_id = int(user["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthError("parse") from exc
    if telegram_id <= 0:
        raise AuthError("parse")
    username = user.get("username") if isinstance(user.get("username"), str) else None
    first_name = (
        user.get("first_name") if isinstance(user.get("first_name"), str) else None
    )
    return (
        TelegramIdentity(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name[:64] if first_name else None,
            start_param=params.get("start_param"),
        ),
        received_hash.lower(),
    )


WIDGET_FIELDS = ("id", "first_name", "last_name", "username", "photo_url", "auth_date")


def validate_login_widget(
    data: dict[str, Any],
    bot_token: str,
    *,
    max_age: int = 300,
    now: float | None = None,
) -> tuple[TelegramIdentity, str]:
    """Validates Telegram Login Widget data. Returns (identity, hash)."""
    received_hash = data.get("hash")
    if not isinstance(received_hash, str) or not received_hash:
        raise AuthError("parse")
    fields = {
        k: str(v) for k, v in data.items() if k in WIDGET_FIELDS and v is not None
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise AuthError("bad_hash")
    _check_auth_date(fields.get("auth_date"), max_age, now)
    try:
        telegram_id = int(fields["id"])
    except (KeyError, ValueError) as exc:
        raise AuthError("parse") from exc
    first_name = fields.get("first_name")
    return (
        TelegramIdentity(
            telegram_id=telegram_id,
            username=fields.get("username"),
            first_name=first_name[:64] if first_name else None,
        ),
        received_hash.lower(),
    )


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": self.expires_in,
            "token_type": "Bearer",
        }


class TokenService:
    def __init__(
        self,
        db: Database,
        secret: str,
        access_ttl: int = 900,
        refresh_ttl: int = 30 * 86400,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        self.db = db
        self._secret = secret
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl

    def _encode(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self._secret, algorithm=JWT_ALGORITHM)

    def decode(self, token: str, expected_type: str) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[JWT_ALGORITHM],
                options={"require": ["sub", "exp", "iat", "jti", "typ"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError("bad_token") from exc
        if claims.get("typ") != expected_type:
            raise AuthError("bad_token_type")
        try:
            claims["sub"] = int(claims["sub"])
        except (TypeError, ValueError) as exc:
            raise AuthError("bad_token") from exc
        return claims

    async def issue(self, telegram_id: int) -> TokenPair:
        now = int(time.time())
        refresh_jti = uuid.uuid4().hex
        refresh_exp = now + self.refresh_ttl
        await self.db.add_refresh_token(refresh_jti, telegram_id, refresh_exp)
        access = self._encode(
            {
                "sub": str(telegram_id),
                "iat": now,
                "exp": now + self.access_ttl,
                "jti": uuid.uuid4().hex,
                "sid": refresh_jti,
                "typ": "access",
            },
        )
        refresh = self._encode(
            {
                "sub": str(telegram_id),
                "iat": now,
                "exp": refresh_exp,
                "jti": refresh_jti,
                "typ": "refresh",
            },
        )
        return TokenPair(access, refresh, self.access_ttl)

    async def rotate(self, refresh_token: str) -> TokenPair:
        claims = self.decode(refresh_token, "refresh")
        record = await self.db.get_refresh_token(claims["jti"])
        if record is None or record["telegram_id"] != claims["sub"]:
            raise AuthError("unknown_refresh")
        if record["revoked"]:
            # Reuse of a rotated token signals theft: kill every session.
            await self.db.revoke_all_refresh_tokens(claims["sub"])
            raise AuthError("refresh_reuse")
        if record["expires_at"] < int(time.time()):
            raise AuthError("expired")
        if not await self.db.revoke_refresh_token(claims["jti"]):
            raise AuthError("refresh_race")
        return await self.issue(claims["sub"])

    async def session_active(self, sid: str | None) -> bool:
        if not sid:
            return False
        record = await self.db.get_refresh_token(sid)
        return bool(
            record and not record["revoked"] and record["expires_at"] >= time.time()
        )

    async def revoke(self, sid: str) -> None:
        await self.db.revoke_refresh_token(sid)


def ip_fingerprint(ip: str, key: str) -> str:
    """Keyed hash so logs can correlate abuse without storing raw IPs."""
    return hmac.new(key.encode(), ip.encode(), hashlib.sha256).hexdigest()[:16]
