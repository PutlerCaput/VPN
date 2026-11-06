"""RU payment routing must be explicit, isolated and fail closed."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from config import Settings
from scripts.render_payment_edge import render, write_bundle
from services.yookassa import YooKassaClient, YooKassaError


@pytest.mark.parametrize(
    "url",
    [
        "http://45.138.157.65:3128",
        "https://10.203.0.1:3128",
        "http://login:password@10.203.0.1:3128",
        "http://10.203.0.1",
        "http://10.203.0.1:3128/other",
        "http://10.203.0.1:3128?token=x",
        "http://169.254.169.254:3128",
        "socks5://10.203.0.1:3128",
    ],
)
def test_rejects_unprotected_or_ambiguous_proxy(url: str) -> None:
    with pytest.raises(ValidationError, match="yookassa_proxy_url"):
        Settings(_env_file=None, yookassa_egress="ru_gateway", yookassa_proxy_url=url)


def test_gateway_cannot_silently_turn_into_direct_route() -> None:
    with pytest.raises(ValidationError, match="required for ru_gateway"):
        Settings(_env_file=None, yookassa_egress="ru_gateway")
    with pytest.raises(ValidationError, match="requires yookassa_egress"):
        Settings(_env_file=None, yookassa_proxy_url="http://10.203.0.1:3128")
    settings = Settings(
        _env_file=None,
        yookassa_egress="ru_gateway",
        yookassa_proxy_url="http://10.203.0.1:3128/",
    )
    assert settings.yookassa_proxy_url == "http://10.203.0.1:3128"


async def test_connect_proxy_failure_never_contacts_payment_server_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_requests: list[bytes] = []
    direct_connections: list[bool] = []

    async def reject(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            proxy_requests.append(
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            )
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def unexpected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        direct_connections.append(True)
        writer.close()
        await writer.wait_closed()

    # Both endpoints are loopback fakes. No external requests or charges.
    proxy = await asyncio.start_server(reject, "127.0.0.1", 0)
    target = await asyncio.start_server(unexpected, "127.0.0.1", 0)
    proxy_port = proxy.sockets[0].getsockname()[1]
    target_port = target.sockets[0].getsockname()[1]
    monkeypatch.setenv("NO_PROXY", "*")
    key = "test_key_must_never_be_sent_to_the_proxy"
    client = YooKassaClient(
        "123456",
        key,
        return_url="https://t.me/test_node_bot",
        base_url=f"https://127.0.0.1:{target_port}/v3",
        proxy_url=f"http://127.0.0.1:{proxy_port}",
        timeout=2,
    )
    try:
        with pytest.raises(YooKassaError, match="transport error ProxyError"):
            await client.create_payment(
                payment_id="a" * 32,
                amount_rub=300,
                months=1,
                email=None,
            )
    finally:
        await client.close()
        proxy.close()
        target.close()
        await proxy.wait_closed()
        await target.wait_closed()
    assert len(proxy_requests) == 1
    assert proxy_requests[0].startswith(f"CONNECT 127.0.0.1:{target_port} ".encode())
    assert b"authorization:" not in proxy_requests[0].lower()
    assert key.encode() not in proxy_requests[0]
    assert direct_connections == []


@pytest.mark.parametrize(
    ("domain", "ru_ip", "de_ip"),
    [
        ("pay.example.org;include", "10.203.0.1", "10.203.0.2"),
        ("pay.example.org", "0.0.0.0", "10.203.0.2"),  # noqa: S104  # rejection test
        ("pay.example.org", "10.203.0.1", "10.203.0.1"),
    ],
)
def test_renderer_rejects_injection_and_public_tunnel_binds(
    domain: str,
    ru_ip: str,
    de_ip: str,
) -> None:
    with pytest.raises(ValueError, match="domain must|RFC1918|must differ"):
        render(domain, ru_ip, de_ip)


def test_render_never_overwrites_installed_or_prepared_files(tmp_path: Any) -> None:
    files = render("pay.example.org", "10.203.0.1", "10.203.0.2")
    output = tmp_path / "candidate"
    write_bundle(output, files)
    with pytest.raises(FileExistsError):
        write_bundle(output, files)
    assert (output / "ru-squid.conf").read_text() == files["ru-squid.conf"]


def test_de_webhook_proxy_is_standalone_and_does_not_touch_caddy() -> None:
    files = render("pay.example.org", "10.203.0.1", "10.203.0.2")
    config = files["de-nginx.conf"]
    service = files["node-payment-backend.service"]

    assert "worker_processes 1;" in config
    assert "listen 10.203.0.2:18081;" in config
    assert "client_body_temp_path /run/node-payment-backend/client_temp;" in config
    assert "proxy_temp_path /run/node-payment-backend/proxy_temp;" in config
    assert "fastcgi_temp_path /run/node-payment-backend/fastcgi_temp;" in config
    assert "uwsgi_temp_path /run/node-payment-backend/uwsgi_temp;" in config
    assert "scgi_temp_path /run/node-payment-backend/scgi_temp;" in config
    assert "allow 10.203.0.1;" in config
    assert "location = /yookassa/webhook" in config
    assert "proxy_pass http://127.0.0.1:8080;" in config
    assert "listen 80" not in config
    assert "BindsTo=wg-quick@wg-payments.service" in service
    assert "User=www-data" in service
    assert "/run/node-payment-backend/proxy_temp" in service
    assert "/run/node-payment-backend/fastcgi_temp" in service
    assert "/run/node-payment-backend/uwsgi_temp" in service
    assert "/run/node-payment-backend/scgi_temp" in service
