"""Security regression suite: real attacks fired at the live app, each must fail.

These lock in the properties an aggressive pentest confirmed on 10 Nov 2026 —
so a later change that reopens one of these holes (accepting ``alg=none``,
trusting an unsigned webhook, dropping a type check, losing a per-user lock)
breaks a test instead of leaking money or access. Every test asserts the
ATTACK is rejected; none asserts a vulnerability.

Covered: JWT forgery (none-alg, wrong secret, access/refresh type confusion),
Telegram initData and login-widget forgery, refresh-token rotation theft,
IDOR via a body-supplied telegram_id, webhook signature bypass and amount
downgrade, out-of-range payment amounts, unauthenticated access, and the money
races (parallel refresh rotation, double activation, refund-versus-activation).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import jwt
import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.conftest import make_init_data
from webhook_server import create_webhook_app

API = "/api/v1"


async def _good_token(client: TestClient, bot_token: str, tid: int) -> dict[str, str]:
    init = make_init_data(bot_token, tid)
    r = await client.post(f"{API}/auth/telegram", json={"initData": init})
    assert r.status == 200, await r.text()
    body = await r.json()
    return body


@pytest.fixture
async def live(services: Any):
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        yield services, client


async def test_jwt_none_algorithm_is_rejected(live: Any) -> None:
    services, client = live
    # Forge an unsigned token claiming to be user 999.
    header = {"alg": "none", "typ": "JWT"}
    claims = {
        "sub": "999",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "jti": "x" * 32,
        "sid": "s" * 32,
        "typ": "access",
    }

    def b64(d: dict[str, Any]) -> str:
        import base64

        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    forged = f"{b64(header)}.{b64(claims)}."
    r = await client.get(f"{API}/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status == 401, "alg=none forgery accepted!"


async def test_jwt_signed_with_wrong_secret_is_rejected(live: Any) -> None:
    services, client = live
    claims = {
        "sub": "999",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "jti": "x" * 32,
        "sid": "s" * 32,
        "typ": "access",
    }
    forged = jwt.encode(claims, "attacker-secret-attacker-secret-32", algorithm="HS256")
    r = await client.get(f"{API}/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status == 401, "token signed with wrong secret accepted!"


async def test_refresh_token_used_as_access_is_rejected(live: Any) -> None:
    services, client = live
    bot_token = services.settings.bot_token.get_secret_value()
    tokens = await _good_token(client, bot_token, 700001)
    r = await client.get(
        f"{API}/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert r.status == 401, "refresh token accepted as access token (typ confusion)!"


async def test_access_token_cannot_rotate(live: Any) -> None:
    services, client = live
    bot_token = services.settings.bot_token.get_secret_value()
    tokens = await _good_token(client, bot_token, 700002)
    r = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert r.status == 401, "access token accepted at /auth/refresh!"


async def test_refresh_replay_after_rotation_kills_session(live: Any) -> None:
    services, client = live
    bot_token = services.settings.bot_token.get_secret_value()
    tokens = await _good_token(client, bot_token, 700003)
    old_refresh = tokens["refresh_token"]
    # First rotation: legitimate.
    r1 = await client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    assert r1.status == 200
    new = await r1.json()
    # Replay the old (now rotated) refresh token: must fail AND revoke the new one.
    r2 = await client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    assert r2.status == 401, "rotated refresh token replay accepted!"
    # Theft response: the freshly issued token must now be dead too.
    r3 = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": new["refresh_token"]}
    )
    assert r3.status == 401, "reuse did not revoke the whole session chain!"


async def test_initdata_tampered_user_is_rejected(live: Any) -> None:
    services, client = live
    bot_token = services.settings.bot_token.get_secret_value()
    # Valid initData for 111, then swap the user id to 222 keeping the hash.
    init = make_init_data(bot_token, 111)
    tampered = init.replace("111", "222")
    r = await client.post(f"{API}/auth/telegram", json={"initData": tampered})
    assert r.status == 401, "tampered initData user id accepted!"


async def test_initdata_with_foreign_hmac_is_rejected(live: Any) -> None:
    services, client = live
    # Attacker signs initData with their own "bot token".
    evil = make_init_data("evil-bot-token-evil-bot-token-123", 333)
    r = await client.post(f"{API}/auth/telegram", json={"initData": evil})
    assert r.status == 401, "initData signed with attacker key accepted!"


async def test_login_widget_forgery_is_rejected(live: Any) -> None:
    services, client = live
    # No initData -> login-widget path. Attacker supplies fields + a guessed hash.
    r = await client.post(
        f"{API}/auth/telegram",
        json={
            "id": 444,
            "first_name": "X",
            "auth_date": str(int(time.time())),
            "hash": "0" * 64,
        },
    )
    assert r.status == 401, "login widget with forged hash accepted!"


async def test_no_endpoint_takes_a_foreign_id(live: Any) -> None:
    """IDOR surface: an authenticated user must only ever touch their own rows."""
    services, client = live
    bot_token = services.settings.bot_token.get_secret_value()
    tokens = await _good_token(client, bot_token, 700010)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    # Try to smuggle another telegram_id in the body of state-changing calls.
    for path, payload in [
        ("/payments/create", {"months": 1, "method": "usdt", "telegram_id": 1}),
        ("/payments/in-bot", {"months": 1, "tier": "standard", "telegram_id": 1}),
    ]:
        r = await client.post(f"{API}{path}", json=payload, headers=headers)
        # Whatever the business outcome, it must act on 700010, never on id 1.
        assert r.status != 500, f"{path} crashed on injected telegram_id"
    u1 = await services.db.get_user(1)
    assert u1 is None, "a foreign telegram_id in the body created/changed a user!"


async def test_cryptopay_webhook_without_signature_is_rejected(live: Any) -> None:
    services, client = live
    body = json.dumps({"update_type": "invoice_paid", "payload": {"invoice_id": 1}})
    # The webhook route is at /cryptopay/webhook, not under /api/v1.
    r = await client.post("/cryptopay/webhook", data=body)
    assert r.status == 401, "cryptopay webhook accepted an unsigned body!"


async def test_cryptopay_webhook_wrong_signature_is_rejected(live: Any) -> None:
    services, client = live
    body = b'{"update_type":"invoice_paid","payload":{"invoice_id":1}}'
    sig = hmac.new(b"wrong-key", body, hashlib.sha256).hexdigest()
    r = await client.post(
        "/cryptopay/webhook",
        data=body,
        headers={"crypto-pay-api-signature": sig},
    )
    assert r.status == 401, "cryptopay webhook accepted a wrong signature!"


async def test_payment_months_out_of_range_is_refused(live: Any) -> None:
    services, client = live
    bot_token = services.settings.bot_token.get_secret_value()
    tokens = await _good_token(client, bot_token, 700020)
    await client.post(
        f"{API}/legal/accept",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    for months in (0, -1, 999, 2**31, 1.5):
        r = await client.post(
            f"{API}/payments/create",
            json={"months": months, "method": "usdt"},
            headers=headers,
        )
        assert r.status in (400, 409, 422), f"months={months} not refused ({r.status})"


async def test_unauth_protected_path_is_401(live: Any) -> None:
    services, client = live
    for path in ("/me", "/config", "/payments/history", "/servers"):
        r = await client.get(f"{API}{path}")
        assert r.status == 401, f"{path} reachable without a token ({r.status})"


async def test_concurrent_refresh_rotation_only_one_wins(live: Any) -> None:
    """Two parallel rotations of the same refresh token: at most one new pair."""
    services, client = live
    bot_token = services.settings.bot_token.get_secret_value()
    tokens = await _good_token(client, bot_token, 700030)
    rt = tokens["refresh_token"]
    results = await asyncio.gather(
        *[
            client.post(f"{API}/auth/refresh", json={"refresh_token": rt})
            for _ in range(8)
        ]
    )
    ok = [r for r in results if r.status == 200]
    assert len(ok) <= 1, f"{len(ok)} parallel rotations of one token succeeded!"


async def test_concurrent_payment_activation_credits_once(live: Any) -> None:
    """Fire the same paid order through provisioning in parallel: one extension."""
    services, _ = live
    await services.db.create_user(700040, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(700040)
    before = int((await services.db.get_user(700040))["expires_at"])
    pid = "c" * 32
    await services.db.create_payment(
        payment_id=pid,
        telegram_id=700040,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )
    await asyncio.gather(
        *[services.provisioning.process_paid_payment(pid) for _ in range(10)]
    )
    after = int((await services.db.get_user(700040))["expires_at"])
    assert after - before == 30 * 86400, f"double activation: +{after - before}s"


async def test_concurrent_refund_and_activation_no_free_time(live: Any) -> None:
    """Refund racing activation must not leave the period granted."""
    services, _ = live
    await services.db.create_user(700050, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(700050)
    before = int((await services.db.get_user(700050))["expires_at"])
    pid = "d" * 32
    await services.db.create_payment(
        payment_id=pid,
        telegram_id=700050,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )
    await services.provisioning.process_paid_payment(pid)
    # Now refund and re-activate in parallel.
    await asyncio.gather(
        services.provisioning.revoke_refunded_payment(pid),
        services.provisioning.process_paid_payment(pid),
        return_exceptions=True,
    )
    user = await services.db.get_user(700050)
    # Either refunded (back to before) or paid once (before + 30d), never +60d.
    delta = int(user["expires_at"]) - before
    assert delta in (0, 30 * 86400), f"refund race left delta={delta}s"


async def test_sbp_webhook_amount_downgrade_is_rejected(live: Any) -> None:
    """A signed SBP notice that pays less than the order must not activate it."""
    services, client = live
    if services.sbp.is_manual:
        pytest.skip("SBP in manual mode has no webhook")
    await services.db.create_user(700060, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(700060)
    pid = "e" * 32
    await services.db.create_payment(
        payment_id=pid,
        telegram_id=700060,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )
    # Underpay: 1 RUB against a 300 RUB order.
    payload = json.dumps(
        {"payment_id": pid, "status": "paid", "amount": 1, "provider_id": "x"}
    ).encode()
    sig = services.sbp.sign(payload) if hasattr(services.sbp, "sign") else None
    headers = {"X-Signature": sig} if sig else {}
    r = await client.post("/sbp/webhook", data=payload, headers=headers)
    assert r.status in (401, 422), f"underpaid SBP accepted ({r.status})"
    assert (await services.db.get_payment(pid))["status"] != "paid"
