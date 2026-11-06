from __future__ import annotations

from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from api import create_api_app


async def test_auth_endpoint_limited_to_10_per_ip(services: Any) -> None:
    app = create_api_app(services)
    async with TestClient(TestServer(app)) as client:
        statuses = []
        for _ in range(11):
            response = await client.post("/auth/telegram", json={"initData": "bad"})
            statuses.append(response.status)
        assert statuses[:10] == [401] * 10
        assert statuses[10] == 429
        assert "Retry-After" in (await client.post("/auth/telegram", json={})).headers


async def test_uniform_unauthorized_body(services: Any) -> None:
    app = create_api_app(services)
    async with TestClient(TestServer(app)) as client:
        for payload in ({"initData": "x"}, {"hash": "y"}, {}):
            response = await client.post("/auth/telegram", json=payload)
            assert response.status == 401
            assert await response.json() == {"error": "unauthorized"}
        response = await client.get("/me")
        assert response.status == 401
        assert await response.json() == {"error": "unauthorized"}


async def test_security_headers_present(services: Any) -> None:
    app = create_api_app(services)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/legal/offer")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "max-age=31536000" in response.headers["Strict-Transport-Security"]
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Request-ID"]
