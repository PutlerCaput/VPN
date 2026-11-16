"""Input sanitization, HMAC helpers and an in-memory sliding-window limiter."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from collections import deque

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,32}$")
# Panel identifiers: a UUID (current Hiddify users), plus legacy names
# still present in older rows (u<telegram_id>, n + 20 hex characters).
NODE_USERNAME_PATTERN = (
    r"^(?:u\d{5,20}|n[0-9a-f]{20}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
NODE_USERNAME_RE = re.compile(NODE_USERNAME_PATTERN)
NODE_NAME_CONTEXT = b"node-username/v1"
PAYMENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def sanitize_username(value: str | None) -> str | None:
    """Returns a Telegram username if it is well-formed, otherwise None."""
    if not value:
        return None
    candidate = value.strip().lstrip("@")
    return candidate if USERNAME_RE.fullmatch(candidate) else None


def pseudonymous_node_username(secret: str, telegram_id: int) -> str:
    """Panel name of a new account: n + 20 hex characters of a keyed hash.

    The key is a subkey of the server secret, so the panel alone cannot map the
    name back to the Telegram ID. The same ID always gives the same name, so a
    retried creation finds its account instead of making a second one.
    """
    # Negative ids are accounts made on the site by email (no Telegram behind them).
    if telegram_id == 0:
        raise ValueError("telegram_id must not be zero")
    subkey = hmac.new(secret.encode(), NODE_NAME_CONTEXT, hashlib.sha256).digest()
    digest = hmac.new(subkey, str(telegram_id).encode(), hashlib.sha256).hexdigest()
    return "n" + digest[:20]


def is_valid_node_username(value: str) -> bool:
    if NODE_USERNAME_RE.fullmatch(value) or NODE_USERNAME_RE.fullmatch(value.lower()):
        return True
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def is_valid_payment_id(value: str) -> bool:
    return bool(PAYMENT_ID_RE.fullmatch(value))


def hmac_sha256_hex(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_hmac_sha256(key: bytes, message: bytes, signature: str | None) -> bool:
    """Constant-time comparison of a hex HMAC-SHA256 signature."""
    if not signature or not key:
        return False
    expected = hmac_sha256_hex(key, message)
    return hmac.compare_digest(expected, signature.strip().lower())


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class SlidingWindowLimiter:
    """Per-key sliding-window rate limiter (single process, in memory)."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, now: float | None = None) -> bool:
        """Registers a hit. Returns True if allowed, False if over the limit."""
        current = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        threshold = current - self.window
        while bucket and bucket[0] <= threshold:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        return True

    def retry_after(self, key: str, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        bucket = self._hits.get(key)
        if not bucket or len(bucket) < self.limit:
            return 0.0
        return max(0.0, bucket[0] + self.window - current)

    def prune(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        threshold = current - self.window
        for key in list(self._hits):
            bucket = self._hits[key]
            while bucket and bucket[0] <= threshold:
                bucket.popleft()
            if not bucket:
                del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)
