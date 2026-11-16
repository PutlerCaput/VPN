from __future__ import annotations

import hashlib
import hmac

import uuid

import pytest

from services.security import (
    SlidingWindowLimiter,
    html_escape,
    is_valid_node_username,
    is_valid_payment_id,
    pseudonymous_node_username,
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
    name = pseudonymous_node_username("s" * 64, 123456789)
    assert len(name) == 21
    assert name[0] == "n"
    assert all(ch in "0123456789abcdef" for ch in name[1:])
    assert "123456789" not in name
    assert name == pseudonymous_node_username("s" * 64, 123456789)
    assert name != pseudonymous_node_username("t" * 64, 123456789)
    assert name != pseudonymous_node_username("s" * 64, 123456780)
    assert is_valid_node_username(name)
    assert is_valid_node_username("u12345")  # accounts made before the change
    assert not is_valid_node_username("u1234")
    assert not is_valid_node_username("n" + "a" * 19)
    assert not is_valid_node_username("admin")
    assert not is_valid_node_username("u12345/../x")
    generated = str(uuid.uuid4())
    assert is_valid_node_username(generated)
    assert not is_valid_node_username(generated.replace("-", ""))
    with pytest.raises(ValueError, match="zero"):
        pseudonymous_node_username("s" * 64, 0)


def test_abuse_report_accepts_both_name_formats() -> None:
    from pydantic import ValidationError

    from api.schemas import AbuseReportIn

    AbuseReportIn(node_username="u12345", kinds=["PORT_SCANNING"])
    AbuseReportIn(node_username="n" + "a" * 20, kinds=["PORT_SCANNING"])
    with pytest.raises(ValidationError):
        AbuseReportIn(node_username="n" + "a" * 19, kinds=["PORT_SCANNING"])


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
