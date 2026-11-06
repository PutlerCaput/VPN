from __future__ import annotations

import hashlib
import hmac

import pytest

from services.security import (
    SlidingWindowLimiter,
    html_escape,
    is_valid_node_username,
    is_valid_payment_id,
    node_username_for,
    sanitize_username,
    verify_hmac_sha256,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("john_doe", "john_doe"),
        ("@john_doe", "john_doe"),
        ("  Alice99 ", "Alice99"),
        ("", None),
        (None, None),
        ("a" * 33, None),
        ("bad-name", None),
        ("робот", None),
        ("x'; DROP TABLE users;--", None),
    ],
)
def test_sanitize_username(raw: str | None, expected: str | None) -> None:
    assert sanitize_username(raw) == expected


def test_node_username() -> None:
    assert node_username_for(123456789) == "u123456789"
    assert is_valid_node_username("u12345")
    assert not is_valid_node_username("u1234")
    assert not is_valid_node_username("admin")
    assert not is_valid_node_username("u12345/../x")
    with pytest.raises(ValueError, match="invalid"):
        node_username_for(12)


def test_payment_id() -> None:
    assert is_valid_payment_id("a" * 32)
    assert not is_valid_payment_id("A" * 32)
    assert not is_valid_payment_id("a" * 31)


def test_verify_hmac_sha256() -> None:
    key = b"secret"
    body = b'{"x":1}'
    signature = hmac.new(key, body, hashlib.sha256).hexdigest()
    assert verify_hmac_sha256(key, body, signature)
    assert verify_hmac_sha256(key, body, signature.upper())
    assert not verify_hmac_sha256(key, body + b" ", signature)
    assert not verify_hmac_sha256(b"other", body, signature)
    assert not verify_hmac_sha256(key, body, None)
    assert not verify_hmac_sha256(key, body, "")
    assert not verify_hmac_sha256(b"", body, signature)


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
    assert all(limiter.hit("k", now=t) for t in (0.0, 1.0, 2.0))
    assert not limiter.hit("k", now=3.0)
    assert limiter.retry_after("k", now=3.0) == pytest.approx(57.0)
    assert limiter.hit("other", now=3.0)
    assert limiter.hit("k", now=60.5)
    limiter.prune(now=1000.0)
    assert len(limiter) == 0


def test_html_escape() -> None:
    assert html_escape('<b>"x"&') == "&lt;b&gt;&quot;x&quot;&amp;"
