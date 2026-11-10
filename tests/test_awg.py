"""AmneziaWG: server agent, per-device keys, reconciliation and shared quota."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from config import GIB
from services.awg import (
    QUOTA_STEP_BYTES,
    AwgAgent,
    AwgError,
    AwgInactiveError,
    AwgLimitError,
    AwgService,
    ServerInfo,
    combined_usage,
    generate_keypair,
    generate_psk,
    quantize,
    render_config,
)

ROOT = Path(__file__).resolve().parent.parent
PARAMS = {
    "jc": 5,
    "jmin": 20,
    "jmax": 60,
    "s1": 40,
    "s2": 70,
    "h1": 1001,
    "h2": 2002,
    "h3": 3003,
    "h4": 4004,
}


def load_agent() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "awg_agent", ROOT / "deploy" / "awg" / "agent.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


agent_module = load_agent()


def key(n: int) -> str:
    return base64.b64encode(bytes([n]) * 32).decode()


# =================================================================== agent
class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.dump = ""

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        args = list(args)
        self.calls.append(args)
        if args[1:] == ["genkey"]:
            return key(9) + "\n"
        if args[1:] == ["pubkey"]:
            return key(8) + "\n"
        if "dump" in args:
            return self.dump
        return ""


@pytest.fixture
def agent(tmp_path: Path) -> Any:
    config = agent_module.Config(
        subnet="10.67.0.0/24",
        endpoint_host="203.0.113.5",
        state_dir=tmp_path / "state",
        run_dir=tmp_path,
        token="t" * 40,
    )
    return agent_module.Agent(config, FakeRunner())


def test_params_satisfy_amneziawg_constraints() -> None:
    for _ in range(200):
        p = agent_module.generate_params()
        assert 4 <= p["jc"] <= 8
        assert p["jmin"] < p["jmax"] <= 1280
        assert p["s1"] + 56 != p["s2"]
        headers = [p["h1"], p["h2"], p["h3"], p["h4"]]
        assert len(set(headers)) == 4
        assert all(5 <= h < 2**31 for h in headers)


def test_config_validation() -> None:
    good = agent_module.Config(endpoint_host="203.0.113.5", token="t" * 40)
    good.validate()
    for change in (
        {"subnet": "8.8.8.0/24"},
        {"subnet": "10.0.0.0/30"},
        {"port": 80},
        {"endpoint_host": ""},
        {"endpoint_host": "a b"},
        {"token": "short"},
    ):
        bad = agent_module.Config(
            **{"endpoint_host": "203.0.113.5", "token": "t" * 40, **change}
        )
        with pytest.raises(ValueError, match="AWG_|token"):
            bad.validate()


def test_server_keys_and_params_persist(agent: Any, tmp_path: Path) -> None:
    again = agent_module.Agent(agent.config, FakeRunner())
    assert again.state.private_key == agent.state.private_key == key(9)
    assert again.state.params == agent.state.params
    server = json.loads((tmp_path / "state" / "server.json").read_text())
    assert server["public_key"] == key(8)
    assert oct((tmp_path / "state" / "server.json").stat().st_mode)[-3:] == "600"


def test_upsert_allocates_keeps_and_restores_addresses(agent: Any) -> None:
    a = agent.upsert({"public_key": key(1), "preshared_key": key(2), "enabled": True})
    b = agent.upsert({"public_key": key(3), "preshared_key": key(2), "enabled": True})
    assert (a["address"], b["address"]) == ("10.67.0.2", "10.67.0.3")
    # Re-enabling keeps the address; the bot can ask for its stored address.
    again = agent.upsert(
        {"public_key": key(1), "preshared_key": key(2), "enabled": False}
    )
    assert again["address"] == "10.67.0.2"
    moved = agent.upsert(
        {
            "public_key": key(1),
            "preshared_key": key(2),
            "enabled": True,
            "address": "10.67.0.20",
        }
    )
    assert moved["address"] == "10.67.0.20"
    taken = agent.upsert(
        {
            "public_key": key(4),
            "preshared_key": key(2),
            "enabled": True,
            "address": "10.67.0.3",  # belongs to another peer
        }
    )
    assert taken["address"] == "10.67.0.2"
    disable = [c for c in agent.run.calls if c[-1] == "remove"]
    assert disable
    assert disable[0][-2] == key(1)
    for bad in (
        {"public_key": "x", "preshared_key": key(2), "enabled": True},
        {"public_key": key(1), "preshared_key": key(2), "enabled": "yes"},
        {"public_key": key(1) + "\nPostUp", "preshared_key": key(2), "enabled": True},
    ):
        with pytest.raises(ValueError, match="invalid peer"):
            agent.upsert(bad)


def test_server_conf_lists_enabled_peers_only(agent: Any) -> None:
    agent.upsert({"public_key": key(1), "preshared_key": key(2), "enabled": True})
    agent.upsert({"public_key": key(3), "preshared_key": key(2), "enabled": False})
    conf = agent.server_conf()
    assert f"PublicKey = {key(1)}" in conf
    assert key(3) not in conf
    for name in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
        assert f"\n{name} = " in conf


def test_peers_parse_dump_and_delete(agent: Any) -> None:
    agent.upsert({"public_key": key(1), "preshared_key": key(2), "enabled": True})
    agent.run.dump = (
        "priv\tpub\t39051\toff\n"
        f"{key(1)}\tpsk\t198.51.100.3:5000\t10.67.0.2/32\t1700000000\t1500\t2500\t25\n"
    )
    peers = agent.peers()["peers"]
    assert peers == [
        {
            "public_key": key(1),
            "address": "10.67.0.2",
            "enabled": True,
            "latest_handshake": 1700000000,
            "rx_bytes": 1500,
            "tx_bytes": 2500,
        }
    ]
    assert agent.delete({"public_key": key(1)}) == {"deleted": True}
    assert agent.delete({"public_key": key(1)}) == {"deleted": False}


def test_firewall_isolates_tunnel_clients(agent: Any) -> None:
    agent._firewall("awg0")
    rules = [" ".join(c[1:]) for c in agent.run.calls if c[0].endswith("iptables")]
    assert "-A INPUT -i awg0 -j DROP" in rules
    assert "-A FORWARD -i awg0 -o awg0 -j DROP" in rules
    for target in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert f"-A FORWARD -i awg0 -d {target} -j DROP" in rules
    drops = rules.index("-A FORWARD -i awg0 -d 10.0.0.0/8 -j DROP")
    accept = rules.index("-A FORWARD -i awg0 ! -o awg0 -j ACCEPT")
    assert drops < accept
    assert any("MASQUERADE" in rule for rule in rules)


def test_http_api_requires_the_token(agent: Any) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent_module.make_handler(agent))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path: str, body: Any = None, token: str | None = "t" * 40) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data)  # noqa: S310
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            response = opener.open(request, timeout=5)
            return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        assert call("/healthz", token=None) == (200, {"ok": True})
        assert call("/v1/info", token=None)[0] == 401
        assert call("/v1/info", token="wrong")[0] == 401
        status, info = call("/v1/info")
        assert status == 200
        assert info["public_key"] == key(8)
        assert set(info["params"]) == set(PARAMS)
        status, created = call(
            "/v1/peers/upsert",
            {"public_key": key(1), "preshared_key": key(2), "enabled": True},
        )
        assert (status, created["address"]) == (200, "10.67.0.2")
        assert call("/v1/peers/upsert", {"public_key": "bad"})[0] == 400
        assert call("/v1/nope", {})[0] == 404
    finally:
        server.shutdown()


# ================================================================= service
class FakeAgent:
    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}
        self.next_host = 2
        self.calls: list[tuple[str, str]] = []

    async def info(self) -> ServerInfo:
        return ServerInfo(key(8), "203.0.113.5", 39051, 1280, "1.1.1.1", PARAMS)

    async def peers(self) -> dict[str, dict[str, Any]]:
        return {k: {"public_key": k, **v} for k, v in self.state.items()}

    async def upsert(
        self,
        public_key: str,
        preshared_key: str,
        enabled: bool,
        address: str | None = None,
    ) -> str:
        self.calls.append(("upsert", public_key))
        current = self.state.get(public_key)
        if address is None:
            address = current["address"] if current else f"10.67.0.{self.next_host}"
            self.next_host += 1
        counters = current or {"rx_bytes": 0, "tx_bytes": 0}
        self.state[public_key] = {
            "address": address,
            "enabled": enabled,
            "rx_bytes": counters["rx_bytes"],
            "tx_bytes": counters["tx_bytes"],
        }
        return address

    async def delete(self, public_key: str) -> None:
        self.calls.append(("delete", public_key))
        self.state.pop(public_key, None)

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_agent() -> FakeAgent:
    return FakeAgent()


@pytest.fixture
def awg(services: Any, fake_agent: FakeAgent, marzban: Any) -> AwgService:
    service = AwgService(services.db, fake_agent, marzban, services.settings)  # type: ignore[arg-type]
    services.awg = service
    return service


async def _active(services: Any, telegram_id: int, tier: str = "standard") -> Any:
    await services.db.create_user(telegram_id, "user", None)
    await services.provisioning.accept_offer_and_start_trial(telegram_id)
    payment = await services.billing.create(telegram_id, 1, "sbp", tier)
    await services.provisioning.process_paid_payment(payment.payment_id)
    return await services.db.get_user(telegram_id)


def test_keys_are_real_x25519_pairs() -> None:
    private, public = generate_keypair()
    raw = base64.b64decode(private)
    assert len(raw) == 32
    derived = (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    assert base64.b64decode(public) == derived
    assert len(base64.b64decode(generate_psk())) == 32
    assert generate_keypair()[0] != private


def test_client_config_has_every_field() -> None:
    device = {"private_key": key(1), "preshared_key": key(2), "address": "10.67.0.7"}
    conf = render_config(
        device, ServerInfo(key(8), "203.0.113.5", 39051, 1280, "1.1.1.1", PARAMS)
    )
    assert "Address = 10.67.0.7/32" in conf
    assert "Endpoint = 203.0.113.5:39051" in conf
    assert "AllowedIPs = 0.0.0.0/0, ::/0" in conf
    assert "Jc = 5" in conf
    assert "H4 = 4004" in conf
    assert f"PresharedKey = {key(2)}" in conf


async def test_device_limit_follows_the_tier(services: Any, awg: AwgService) -> None:
    await _active(services, 640001)
    first = await awg.add_device(640001)
    second = await awg.add_device(640001)
    assert (first["name"], second["name"]) == ("Устройство 1", "Устройство 2")
    with pytest.raises(AwgLimitError):
        await awg.add_device(640001)
    assert await awg.remove_device(640001, int(first["id"]))
    third = await awg.add_device(640001)
    assert third["name"] == "Устройство 1"


async def test_inactive_user_cannot_add_and_others_cannot_touch(
    services: Any, awg: AwgService
) -> None:
    await services.db.create_user(640002, "user", None)
    with pytest.raises(AwgInactiveError):
        await awg.add_device(640002)
    await _active(services, 640003)
    device = await awg.add_device(640003)
    assert not await awg.remove_device(640002, int(device["id"]))
    assert await awg.config_for(640002, int(device["id"])) is None
    conf = await awg.config_for(640003, int(device["id"]))
    assert conf is not None
    assert device["private_key"] in conf


async def test_reconcile_follows_the_subscription(
    services: Any, awg: AwgService, fake_agent: FakeAgent
) -> None:
    await _active(services, 640004)
    device = await awg.add_device(640004)
    pk = str(device["public_key"])
    await services.db.set_user_status(640004, "expired")
    await awg.reconcile()
    assert fake_agent.state[pk]["enabled"] is False
    await services.db.set_user_status(640004, "active")
    await awg.reconcile()
    assert fake_agent.state[pk]["enabled"] is True

    # Agent lost its state: the peer comes back with the stored address.
    fake_agent.state.clear()
    await awg.reconcile()
    assert fake_agent.state[pk]["address"] == device["address"]
    # A key the bot does not know (deleted while the agent was down) goes away.
    fake_agent.state[key(77)] = {"address": "10.67.0.99", "enabled": True}
    await awg.reconcile()
    assert key(77) not in fake_agent.state


async def test_shared_quota_lowers_marzban_and_cuts_awg(
    services: Any, awg: AwgService, fake_agent: FakeAgent, marzban: Any
) -> None:
    user = await _active(services, 640005)
    node = user["node_username"]
    device = await awg.add_device(640005)
    pk = str(device["public_key"])
    limit = 200 * GIB

    def panel(used: int, lifetime: int | None = None) -> None:
        lifetime = lifetime if lifetime is not None else used
        marzban.users[node]["used_traffic"] = used
        marzban.users[node]["lifetime_used_traffic"] = lifetime
        marzban.panel_usage = [
            {
                "username": node,
                "used_traffic": used,
                "lifetime_used_traffic": lifetime,
                "data_limit": marzban.users[node]["data_limit"],
                "status": "active",
            }
        ]

    fake_agent.state[pk]["rx_bytes"] = 30 * GIB
    fake_agent.state[pk]["tx_bytes"] = 20 * GIB
    panel(10 * GIB)
    await awg.reconcile()
    assert marzban.users[node]["data_limit"] == limit - quantize(50 * GIB)
    assert (await services.db.get_awg_usage(640005))["bytes"] == 50 * GIB
    status = await services.provisioning.traffic_status(
        await services.db.get_user(640005)
    )
    assert status is not None
    assert status.used_bytes == 60 * GIB
    assert status.limit_bytes == limit

    # Counters only grow: the next poll adds the difference.
    fake_agent.state[pk]["rx_bytes"] = 180 * GIB
    panel(10 * GIB)
    await awg.reconcile()
    assert (await services.db.get_awg_usage(640005))["bytes"] == 200 * GIB
    assert fake_agent.state[pk]["enabled"] is False  # 10 + 200 >= 200 GB

    # Marzban reset its counter: a new window, AmneziaWG works again.
    panel(0, lifetime=10 * GIB)
    await awg.reconcile()
    assert fake_agent.state[pk]["enabled"] is True
    assert marzban.users[node]["data_limit"] == limit


async def test_agent_restart_counter_reset_is_not_negative(
    services: Any, awg: AwgService, fake_agent: FakeAgent, marzban: Any
) -> None:
    user = await _active(services, 640006)
    device = await awg.add_device(640006)
    pk = str(device["public_key"])
    marzban.panel_usage = [
        {"username": user["node_username"], "used_traffic": 0, "data_limit": 1}
    ]
    fake_agent.state[pk]["rx_bytes"] = 5 * GIB
    await awg.reconcile()
    fake_agent.state[pk]["rx_bytes"] = 1 * GIB  # agent restarted
    await awg.reconcile()
    assert (await services.db.get_awg_usage(640006))["bytes"] == 6 * GIB


async def test_combined_usage_ignores_another_window(services: Any) -> None:
    # The window is Marzban's lifetime minus current usage: 6 - 1 = 5 GiB.
    await services.db.set_awg_usage(640007, 5 * GIB, 3 * GIB)
    assert await combined_usage(services.db, 640007, GIB, 6 * GIB, 10 * GIB) == (
        4 * GIB,
        10 * GIB + quantize(3 * GIB),
    )
    assert await combined_usage(services.db, 640007, GIB, 9 * GIB, 10 * GIB) == (
        GIB,
        10 * GIB,
    )
    assert quantize(1) == QUOTA_STEP_BYTES
    assert quantize(0) == 0


# ================================================================ handlers
def _private_message(owner: int) -> Any:
    from aiogram.types import Message

    message = MagicMock()
    message.__class__ = Message
    message.chat.type = "private"
    message.chat.id = owner
    message.from_user.id = owner
    message.answer = AsyncMock()
    message.answer_document = AsyncMock()
    message.answer_photo = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    return message


def _callback(message: Any, owner: int, data: str) -> Any:
    callback = MagicMock()
    callback.message = message
    callback.from_user.id = owner
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def test_menu_add_get_and_delete_device(services: Any, awg: AwgService) -> None:
    from handlers.amnezia import cb_awg, cmd_amnezia

    await _active(services, 640010)
    message = _private_message(640010)
    await cmd_amnezia(message, services)
    assert "Устройств: 0 из 2" in message.answer.await_args.args[0]

    await cb_awg(_callback(message, 640010, "awg:add"), services)
    document = message.answer_document.await_args.args[0]
    assert document.filename.endswith(".conf")
    assert b"[Interface]" in document.data
    message.answer_photo.assert_awaited()

    device = (await awg.devices(640010))[0]
    await cb_awg(_callback(message, 640010, f"awg:del:{device['id']}"), services)
    assert "Удалить ключ" in message.answer.await_args.args[0]
    await cb_awg(_callback(message, 640010, f"awg:delyes:{device['id']}"), services)
    assert await awg.devices(640010) == []

    # Someone else's device cannot be fetched or deleted.
    await _active(services, 640011)
    other = await awg.add_device(640011)
    await cb_awg(_callback(message, 640010, f"awg:get:{other['id']}"), services)
    assert "не найдено" in message.answer.await_args.args[0]
    await cb_awg(_callback(message, 640010, f"awg:delyes:{other['id']}"), services)
    assert len(await awg.devices(640011)) == 1


async def test_menu_when_not_connected(services: Any) -> None:
    from handlers.amnezia import NOT_CONNECTED, cmd_amnezia

    message = _private_message(640012)
    await cmd_amnezia(message, services)
    assert message.answer.await_args.args[0] == NOT_CONNECTED


def test_amnezia_guide_only_when_connected() -> None:
    from keyboards.onboarding import guide_kb, setup_kb

    without = [b.callback_data for row in setup_kb().inline_keyboard for b in row]
    with_awg = [b.callback_data for row in setup_kb(True).inline_keyboard for b in row]
    assert "setup:guide:amnezia" not in without
    assert "setup:guide:amnezia" in with_awg
    buttons = [b for row in guide_kb("amnezia").inline_keyboard for b in row]
    assert any(b.callback_data == "awg:menu" for b in buttons)
    assert any(
        (b.url or "").startswith("https://github.com/amnezia-vpn/") for b in buttons
    )


async def test_awg_traffic_counts_toward_the_server_total(
    services: Any, awg: AwgService, fake_agent: FakeAgent
) -> None:
    await _active(services, 640019)
    device = await awg.add_device(640019)
    pk = str(device["public_key"])
    fake_agent.state[pk]["rx_bytes"] = 3 * GIB
    fake_agent.state[pk]["tx_bytes"] = 2 * GIB
    await awg.reconcile()
    fake_agent.state[pk]["rx_bytes"] = 4 * GIB
    await awg.reconcile()
    rows = await services.db.server_traffic_since("2000-01-01")
    assert sum(value for _, value in rows) == 6 * GIB


async def test_agent_answer_that_is_not_json_is_an_agent_error() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>upstream down</html>")

    agent = AwgAgent("http://awg.test", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(AwgError, match="malformed"):
        await agent.info()
    await agent.close()
