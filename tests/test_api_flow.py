from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from tests.conftest import make_init_data
from webhook_server import create_webhook_app


def _sign_cryptopay(token: str, body: bytes) -> str:
    return hmac.new(
        hashlib.sha256(token.encode()).digest(), body, hashlib.sha256
    ).hexdigest()


async def test_full_flow(services: Any, monkeypatch: Any) -> None:
    settings = services.settings
    bot_token = settings.bot_token.get_secret_value()
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]

    async def fake_invoice(**kwargs: Any) -> dict[str, Any]:
        return {
            "invoice_id": 9001,
            "bot_invoice_url": "https://t.me/CryptoBot?start=IV9001",
        }

    monkeypatch.setattr(services.cryptopay, "create_invoice", fake_invoice)

    async with TestClient(TestServer(app)) as client:
        init_data = make_init_data(bot_token, 880088)
        response = await client.post(
            "/api/v1/auth/telegram", json={"initData": init_data}
        )
        assert response.status == 200
        tokens = await response.json()
        # Replay of the same initData is rejected.
        replay = await client.post(
            "/api/v1/auth/telegram", json={"initData": init_data}
        )
        assert replay.status == 401
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        me = await (await client.get("/api/v1/me", headers=headers)).json()
        assert me["telegram_id"] == 880088
        assert me["offer_accepted"] is False
        assert "config_link" not in me

        assert (await client.get("/api/v1/config", headers=headers)).status == 403
        pay = await client.post(
            "/api/v1/payments/create",
            json={"months": 1, "method": "usdt"},
            headers=headers,
        )
        assert pay.status == 409

        accept = await (
            await client.post("/api/v1/legal/accept", headers=headers)
        ).json()
        assert accept["trial"] == "provisioned"
        config = await (await client.get("/api/v1/config", headers=headers)).json()
        assert config["config_link"].startswith("vless://")

        pay = await client.post(
            "/api/v1/payments/create",
            json={"months": 1, "method": "usdt"},
            headers=headers,
        )
        assert pay.status == 201
        payment = await pay.json()
        assert payment["amount_usdt"] == "3.50"

        sub_before = await (
            await client.get("/api/v1/subscription", headers=headers)
        ).json()

        body = json.dumps(
            {
                "update_type": "invoice_paid",
                "payload": {
                    "invoice_id": 9001,
                    "status": "paid",
                    "asset": "USDT",
                    "amount": "3.50",
                },
            },
        ).encode()
        bad = await client.post(
            "/cryptopay/webhook", data=body, headers={"crypto-pay-api-signature": "00"}
        )
        assert bad.status == 401
        signature = _sign_cryptopay(settings.crypto_pay_token.get_secret_value(), body)
        for _ in range(2):  # duplicate delivery must be harmless
            ok = await client.post(
                "/cryptopay/webhook",
                data=body,
                headers={"crypto-pay-api-signature": signature},
            )
            assert ok.status == 200

        sub_after = await (
            await client.get("/api/v1/subscription", headers=headers)
        ).json()
        assert sub_after["expires_at"] - sub_before["expires_at"] == 30 * 86400
        history = await (
            await client.get("/api/v1/payments/history", headers=headers)
        ).json()
        assert history[0]["status"] == "paid"

        refreshed = await (
            await client.post(
                "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
        ).json()
        assert refreshed["refresh_token"] != tokens["refresh_token"]
        new_headers = {"Authorization": f"Bearer {refreshed['access_token']}"}
        # The rotated-away session no longer authenticates.
        assert (await client.get("/api/v1/me", headers=headers)).status == 401

        await services.provisioning.autoban(
            880088, None, "admin", {"flags": ["DDOS_ATTACK"]}
        )
        assert (
            await client.get("/api/v1/me", headers=new_headers)
        ).status == 401  # sessions revoked on ban

        health = await client.get("/healthz")
        assert health.status == 200


async def test_banned_gets_403_except_me(services: Any) -> None:
    bot_token = services.settings.bot_token.get_secret_value()
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        tokens = await (
            await client.post(
                "/api/v1/auth/telegram",
                json={"initData": make_init_data(bot_token, 990099)},
            )
        ).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        await services.db.set_user_status(990099, "banned")
        assert (await client.get("/api/v1/me", headers=headers)).status == 200
        response = await client.get("/api/v1/subscription", headers=headers)
        assert response.status == 403
        assert await response.json() == {"error": "banned"}
        assert (await client.post("/api/v1/auth/logout", headers=headers)).status == 204
        assert (await client.get("/api/v1/me", headers=headers)).status == 401


async def test_referral_via_start_param(services: Any) -> None:
    services.settings.referral_acquisition_enabled = True
    bot_token = services.settings.bot_token.get_secret_value()
    await services.db.create_user(111111, "inviter", None)
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/api/v1/auth/telegram",
            json={"initData": make_init_data(bot_token, 222222, start_param="111111")},
        )
    assert (await services.db.get_user(222222))["referred_by"] == 111111


async def test_disabled_referrals_hide_links_and_preserve_existing(
    services: Any,
) -> None:
    bot_token = services.settings.bot_token.get_secret_value()
    await services.db.create_user(111111, "inviter", None)
    await services.db.create_user(333333, "existing", 111111)
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        for uid in (222222, 333333, 111111):
            response = await client.post(
                "/api/v1/auth/telegram",
                json={"initData": make_init_data(bot_token, uid, start_param="111111")},
            )
            assert response.status == 200
            tokens = await response.json()
            headers = {"Authorization": f"Bearer {tokens['access_token']}"}
            me = await (await client.get("/api/v1/me", headers=headers)).json()
            referral = await (
                await client.get("/api/v1/referral", headers=headers)
            ).json()
            assert me["referral_enabled"] is False
            assert me["referral_link"] == ""
            assert referral["enabled"] is False
            assert referral["link"] == ""
            if uid == 111111:
                assert referral["invited_count"] == 1
    assert (await services.db.get_user(222222))["referred_by"] is None
    assert (await services.db.get_user(333333))["referred_by"] == 111111
