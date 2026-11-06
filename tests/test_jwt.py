from __future__ import annotations

import time

import jwt
import pytest

from api.auth import AuthError, TokenService
from db.database import Database

SECRET = "b" * 64


async def test_issue_and_decode(db: Database) -> None:
    tokens = TokenService(db, SECRET)
    pair = await tokens.issue(42)
    claims = tokens.decode(pair.access_token, "access")
    assert claims["sub"] == 42
    assert claims["exp"] - claims["iat"] == 900
    assert await tokens.session_active(claims["sid"])
    with pytest.raises(AuthError):
        tokens.decode(pair.access_token, "refresh")


async def test_rotation_revokes_old_and_detects_reuse(db: Database) -> None:
    tokens = TokenService(db, SECRET)
    first = await tokens.issue(42)
    second = await tokens.rotate(first.refresh_token)
    assert second.refresh_token != first.refresh_token
    old_sid = tokens.decode(first.access_token, "access")["sid"]
    assert not await tokens.session_active(old_sid)
    with pytest.raises(AuthError) as exc:
        await tokens.rotate(first.refresh_token)
    assert exc.value.reason == "refresh_reuse"
    # Reuse kills every session of the user, including the fresh one.
    new_sid = tokens.decode(second.access_token, "access")["sid"]
    assert not await tokens.session_active(new_sid)


async def test_rejects_forged_and_expired(db: Database) -> None:
    tokens = TokenService(db, SECRET)
    forged = jwt.encode(
        {
            "sub": "42",
            "iat": 0,
            "exp": int(time.time()) + 60,
            "jti": "x",
            "typ": "access",
        },
        "c" * 64,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        tokens.decode(forged, "access")
    expired = jwt.encode(
        {"sub": "42", "iat": 0, "exp": 1, "jti": "x", "typ": "access"},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        tokens.decode(expired, "access")
    none_alg = jwt.encode({"sub": "42"}, key=None, algorithm="none")
    with pytest.raises(AuthError):
        tokens.decode(none_alg, "access")


async def test_revoke(db: Database) -> None:
    tokens = TokenService(db, SECRET)
    pair = await tokens.issue(7)
    sid = tokens.decode(pair.access_token, "access")["sid"]
    await tokens.revoke(sid)
    assert not await tokens.session_active(sid)
    with pytest.raises(AuthError):
        await tokens.rotate(pair.refresh_token)


def test_short_secret_rejected(db: Database) -> None:
    with pytest.raises(ValueError, match="32"):
        TokenService(db, "short")
