"""Hiddify Manager API v2 client: user create, UUID rotation, usage list."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from services.hiddify import HiddifyClient, HiddifyError, HiddifyNotFound

API_KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ADMIN = "secretadmin"
ORIGIN = "https://panel.example.com"
USER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _client(handler: Any) -> HiddifyClient:
    return HiddifyClient(
        ORIGIN,
        API_KEY,
        ADMIN,
        user_proxy_path="secretuser",
        subscription_base_url="https://sub.example.com",
        transport=httpx.MockTransport(handler),
    )


def test_admin_and_panel_paths_include_proxy_path() -> None:
    client = HiddifyClient(ORIGIN, API_KEY, ADMIN)
    assert client._admin("user") == "/secretadmin/api/v2/admin/user/"
    assert client._admin("user", USER) == f"/secretadmin/api/v2/admin/user/{USER}/"
    assert client._panel("ping") == "/secretadmin/api/v2/panel/ping/"
    assert client._panel("info") == "/secretadmin/api/v2/panel/info/"


@pytest.mark.asyncio
async def test_create_user_posts_uuid_and_stores_subscription_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers.get("Hiddify-API-Key") == API_KEY
        body = json.loads(request.content)
        return httpx.Response(200, json=body)

    client = _client(handler)
    created = await client.create_user(USER, expire=2_000_000_000, tier="standard")
    await client.close()
    assert seen[0].method == "POST"
    assert seen[0].url.path == f"/{ADMIN}/api/v2/admin/user/"
    payload = json.loads(seen[0].content)
    assert payload["uuid"] == USER
    assert payload["usage_limit_GB"] == 100
    assert payload["mode"] == "monthly"
    assert created["uuid"] == USER
    assert created["username"] == USER
    assert created["status"] == "active"
    assert created["subscription_url"] == f"https://sub.example.com/secretuser/{USER}/"


@pytest.mark.asyncio
async def test_revoke_subscription_patches_a_new_uuid() -> None:
    store = {
        USER: {
            "uuid": USER,
            "name": USER,
            "enable": True,
            "is_active": True,
            "usage_limit_GB": 200,
            "mode": "monthly",
            "package_days": 30,
            "start_date": "2026-11-16",
            "current_usage_GB": 1,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PATCH" and path.endswith(f"/user/{USER}/"):
            body = json.loads(request.content)
            new_uuid = body["uuid"]
            record = {**store.pop(USER), **body}
            store[new_uuid] = record
            return httpx.Response(200, json=record)
        return httpx.Response(404, json={"msg": "User not found"})

    client = _client(handler)
    result = await client.revoke_subscription(USER)
    await client.close()
    assert result["uuid"] != USER
    assert uuid.UUID(result["uuid"]).version == 4
    assert result["uuid"] in store
    assert USER not in store
    assert result["subscription_url"] == (
        f"https://sub.example.com/secretuser/{result['uuid']}/"
    )


@pytest.mark.asyncio
async def test_get_user_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"msg": "User not found"})

    client = _client(handler)
    with pytest.raises(HiddifyNotFound):
        await client.get_user(USER)
    await client.close()


@pytest.mark.asyncio
async def test_create_user_rejects_non_uuid() -> None:
    client = HiddifyClient(ORIGIN, API_KEY, ADMIN)
    with pytest.raises(ValueError, match="invalid node uuid"):
        await client.create_user("n0123456789abcdef0123", tier="standard")
    await client.close()


@pytest.mark.asyncio
async def test_get_users_usage_empty_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"msg": "You have no user"})

    client = _client(handler)
    assert await client.get_users_usage() == []
    await client.close()


@pytest.mark.asyncio
async def test_existing_user_on_create_is_updated() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(400, json={"detail": "The user exists"})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"uuid": USER, "enable": True, "is_active": True, **body},
        )

    client = _client(handler)
    data = await client.create_user(USER, expire=2_000_000_000, tier="standard")
    await client.close()
    assert data["uuid"] == USER
    assert any(item.startswith("PATCH ") for item in calls)


@pytest.mark.asyncio
async def test_get_core_from_panel_info_and_ping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/panel/ping/"):
            return httpx.Response(200, json={"msg": "GET: PONG"})
        if request.url.path.endswith("/panel/info/"):
            return httpx.Response(200, json={"version": "14.0.5"})
        return httpx.Response(404, json={})

    client = _client(handler)
    core = await client.get_core()
    await client.close()
    assert core == {"version": "14.0.5", "started": True}


@pytest.mark.asyncio
async def test_retry_on_server_error_then_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = _client(handler)
    with pytest.raises(HiddifyError, match="failed after retries"):
        await client.get_user(USER)
    await client.close()
