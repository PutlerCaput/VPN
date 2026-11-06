"""Input sanitization, HMAC helpers and an in-memory sliding-window limiter."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import deque

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,32}$")
NODE_USERNAME_RE = re.compile(r"^u\d{5,20}$")
PAYMENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def sanitize_username(value: str | None) -> str | None:
    """Returns a Telegram username if it is well-formed, otherwise None."""
    if not value:
        return None
    candidate = value.strip().lstrip("@")
    return candidate if USERNAME_RE.fullmatch(candidate) else None


def node_username_for(telegram_id: int) -> str:
    name = f"u{telegram_id}"
    if not NODE_USERNAME_RE.fullmatch(name):
        raise ValueError("telegram_id produces an invalid node username")
    return name


def is_valid_node_username(value: str) -> bool:
    return bool(NODE_USERNAME_RE.fullmatch(value))


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
