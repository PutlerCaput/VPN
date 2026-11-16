"""Entry sharding: segments, filtering, anti-parsing and the /sub endpoint."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections import Counter
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from config import Settings
from db.database import Database
from services.hiddify import HiddifyError
from services.sharding import (
    FanoutGuard,
    SegmentPlan,
    assign_segment,
    derive_key,
    filter_subscription,
    link_host,
    resolve_segment,
    sharded_subscription_url,
    with_link_fingerprint,
)

KEY = derive_key("k" * 64)
RU_A = "198.51.100.10"
RU_B = "198.51.100.20"
DE = "203.0.113.5"
PLAN = SegmentPlan.build({"ru-a": [RU_A], "ru-b": [RU_B]}, [DE])
NODE = "8d3c4f0e-0000-4000-8000-00000000abcd"


def _vless(host: str, port: int = 443, **query: str) -> str:
    params = "&".join(f"{k}={v}" for k, v in {"security": "reality", **query}.items())
    return f"vless://{NODE}@{host}:{port}?{params}#{host}"


def _vmess(host: str) -> str:
    raw = json.dumps({"v": "2", "add": host, "port": "443", "id": NODE})
    return "vmess://" + base64.b64encode(raw.encode()).decode()


# ---------------------------------------------------------------- assignment
def test_assignment_is_deterministic_and_spread() -> None:
    names = ["ru-a", "ru-b", "ru-c"]
    first = [assign_segment(i, names, KEY) for i in range(3000)]
    assert first == [assign_segment(i, list(reversed(names)), KEY) for i in range(3000)]
    counts = Counter(first)
    assert set(counts) == set(names)
    assert min(counts.values()) > 800  # roughly a third each


def test_assignment_depends_on_the_key() -> None:
    names = ["ru-a", "ru-b"]
    other = derive_key("z" * 64)
    a = [assign_segment(i, names, KEY) for i in range(200)]
    b = [assign_segment(i, names, other) for i in range(200)]
    assert a != b


def test_assignment_needs_segments() -> None:
    with pytest.raises(ValueError, match="no entry segments"):
        assign_segment(1, [], KEY)


def test_plan_isolation_strict_and_loose() -> None:
    assert PLAN.keeps("ru-a", RU_A)
    assert PLAN.keeps("ru-a", DE)  # shared
    assert not PLAN.keeps("ru-a", RU_B)  # another segment: never
    assert not PLAN.keeps("ru-a", "unknown.example.net")  # strict
    loose = SegmentPlan.build({"ru-a": [RU_A], "ru-b": [RU_B]}, strict=False)
    assert loose.keeps("ru-a", "unknown.example.net")
    assert not loose.keeps("ru-a", RU_B)


# ------------------------------------------------------------------ filtering
def test_link_host_reads_common_schemes() -> None:
    assert link_host(_vless(RU_A)) == RU_A
    assert link_host(f"hysteria2://secret@{RU_B}:36099?obfs=salamander#h2") == RU_B
    assert link_host("hy2://secret@[2001:db8::1]:443#h2") == "2001:db8::1"
    assert link_host(_vmess(DE)) == DE
    assert link_host("unknown://x") is None


def test_filter_plain_links_hides_other_segments() -> None:
    body = "\n".join(
        [_vless(RU_A), _vless(RU_B), f"hysteria2://s@{RU_B}:36099#x", _vless(DE)]
    )
    result = filter_subscription(body, PLAN, "ru-a")
    assert result is not None
    assert result.kind == "links"
    assert RU_B not in result.body
    assert RU_A in result.body
    assert DE in result.body
    assert (result.kept, result.removed) == (2, 2)


def test_filter_base64_round_trip() -> None:
    body = base64.b64encode(
        "\n".join([_vless(RU_A), _vless(RU_B), _vmess(RU_B)]).encode()
    ).decode()
    result = filter_subscription(body, PLAN, "ru-b")
    assert result is not None
    assert result.kind == "base64"
    decoded = base64.b64decode(result.body).decode()
    assert RU_A not in decoded
    assert decoded.count("://") == 2


def test_filter_singbox_json_fixes_groups() -> None:
    config = {
        "outbounds": [
            {
                "type": "selector",
                "tag": "select",
                "outbounds": ["a", "b", "auto"],
                "default": "b",
            },
            {"type": "urltest", "tag": "auto", "outbounds": ["a", "b"]},
            {"type": "urltest", "tag": "only-b", "outbounds": ["b"]},
            {
                "type": "vless",
                "tag": "a",
                "server": RU_A,
                "tls": {"enabled": True, "reality": {"enabled": True}},
            },
            {"type": "vless", "tag": "b", "server": RU_B, "tls": {"enabled": True}},
            {"type": "direct", "tag": "direct"},
        ]
    }
    result = filter_subscription(json.dumps(config), PLAN, "ru-a", fingerprint="chrome")
    assert result is not None
    assert result.kind == "singbox"
    data = json.loads(result.body)
    tags = [o["tag"] for o in data["outbounds"]]
    assert "b" not in tags
    assert "only-b" not in tags
    assert "direct" in tags
    select = data["outbounds"][0]
    assert select["outbounds"] == ["a", "auto"]
    assert "default" not in select
    vless_a = next(o for o in data["outbounds"] if o["tag"] == "a")
    assert vless_a["tls"]["utls"] == {"enabled": True, "fingerprint": "chrome"}
    assert RU_B not in result.body


def test_filter_xray_config_list() -> None:
    def cfg(host: str) -> dict[str, Any]:
        return {
            "remarks": host,
            "outbounds": [
                {
                    "protocol": "vless",
                    "tag": "proxy",
                    "settings": {"vnext": [{"address": host, "port": 443}]},
                    "streamSettings": {"realitySettings": {"fingerprint": "firefox"}},
                },
                {"protocol": "freedom", "tag": "direct"},
            ],
        }

    result = filter_subscription(
        json.dumps([cfg(RU_A), cfg(RU_B)]), PLAN, "ru-a", fingerprint="chrome"
    )
    assert result is not None
    assert result.kind == "xray"
    data = json.loads(result.body)
    assert [c["remarks"] for c in data] == [RU_A]
    reality = data[0]["outbounds"][0]["streamSettings"]["realitySettings"]
    assert reality["fingerprint"] == "chrome"


def test_unreadable_formats_are_refused() -> None:
    assert filter_subscription("proxies:\n  - name: a\n", PLAN, "ru-a") is None
    assert filter_subscription("<html>hi</html>", PLAN, "ru-a") is None
    assert filter_subscription('{"x": 1}', PLAN, "ru-a") is None


def test_link_fingerprint() -> None:
    changed = with_link_fingerprint(_vless(RU_A, fp="firefox"), "chrome")
    assert "fp=chrome" in changed
    assert "fp=firefox" not in changed
    assert changed.endswith(f"#{RU_A}")
    plain = f"vless://{NODE}@{RU_A}:80?security=none#x"
    assert with_link_fingerprint(plain, "chrome") == plain
    hy2 = f"hysteria2://s@{RU_A}:443#x"
    assert with_link_fingerprint(hy2, "chrome") == hy2


# --------------------------------------------------------------- anti-parsing
def test_fanout_guard_caps_distinct_sources() -> None:
    guard = FanoutGuard(2, 100.0)
    assert guard.allow("u", "1.1.1.1", now=0)
    assert guard.allow("u", "2.2.2.2", now=1)
    assert guard.allow("u", "1.1.1.1", now=2)  # known source
    assert not guard.allow("u", "3.3.3.3", now=3)
    assert guard.allow("other", "3.3.3.3", now=3)
    assert guard.allow("u", "3.3.3.3", now=200)  # window passed


def test_fanout_guard_is_bounded() -> None:
    guard = FanoutGuard(1, 100.0, max_keys=10)
    for i in range(100):
        guard.allow(f"k{i}", "s", now=0)
    assert len(guard._seen) == 10


# ----------------------------------------------------------- database / table
async def _user(db: Any, telegram_id: int, node: str = NODE) -> None:
    await db.create_user(telegram_id, "user", None)
    await db.set_subscription(
        telegram_id,
        status="active",
        expires_at=int(time.time()) + 86400,
        node_username=node,
        config_link=None,
    )


async def test_segment_table_auto_and_admin(db: Any) -> None:
    await _user(db, 501)
    assert await db.get_entry_segment(501) is None
    assert await db.set_entry_segment(501, "ru-a", "auto")
    assert (await db.get_entry_segment(501))["source"] == "auto"
    assert await db.set_entry_segment(501, "ru-b", "admin")
    # An automatic write never replaces the admin's choice.
    assert not await db.set_entry_segment(501, "ru-a", "auto")
    row = await db.get_entry_segment(501)
    assert (row["segment"], row["source"]) == ("ru-b", "admin")
    counts = await db.entry_segment_counts()
    assert counts == [{"segment": "ru-b", "source": "admin", "users": 1}]
    assert await db.clear_entry_segment(501)
    assert await db.get_entry_segment(501) is None
    with pytest.raises(ValueError, match="source is"):
        await db.set_entry_segment(501, "ru-a", "manual")


async def test_migration_adds_the_table_to_an_old_database(tmp_path: Any) -> None:
    path = str(tmp_path / "old.db")
    old = Database(path)
    await old.connect()
    await old.conn.execute("DROP TABLE entry_segments")
    await old.conn.commit()
    await old.close()
    reopened = Database(path)
    await reopened.connect()
    try:
        async with reopened.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'entry_segments'"
        ) as cur:
            assert await cur.fetchone() is not None
    finally:
        await reopened.close()


async def test_resolve_segment_pins_and_respects_admin(db: Any) -> None:
    await _user(db, 777)
    first = await resolve_segment(db, PLAN, 777, KEY)
    assert first == assign_segment(777, PLAN.names, KEY)
    assert (await db.get_entry_segment(777))["source"] == "auto"
    other = "ru-b" if first == "ru-a" else "ru-a"
    await db.set_entry_segment(777, other, "admin")
    assert await resolve_segment(db, PLAN, 777, KEY) == other
    # The admin pin points at a removed segment: computed one, pin kept.
    await db.set_entry_segment(777, "gone", "admin")
    assert await resolve_segment(db, PLAN, 777, KEY) in PLAN.names
    assert (await db.get_entry_segment(777))["segment"] == "gone"


# ------------------------------------------------------------------- settings
def test_defaults_leave_issuance_unchanged() -> None:
    settings = Settings()
    assert settings.entry_sharding_enabled is False
    assert settings.entry_segments == {}
    assert settings.subscription_fingerprint is None


def test_sharding_needs_segments_and_subscription_delivery() -> None:
    with pytest.raises(ValidationError):
        Settings(entry_sharding_enabled=True, config_delivery="subscription")
    with pytest.raises(ValidationError):
        Settings(entry_sharding_enabled=True, entry_segments={"ru-a": [RU_A]})
    with pytest.raises(ValidationError):
        Settings(entry_segments={"Bad Name": [RU_A]})
    ok = Settings(
        entry_sharding_enabled=True,
        config_delivery="subscription",
        entry_segments={"ru-a": [RU_A]},
    )
    assert ok.entry_sharding_enabled


async def test_provisioning_hands_out_the_sharded_url(provisioning: Any) -> None:
    data = {"uuid": NODE, "subscription_url": f"https://sub.test/p/{NODE}/"}
    provisioning.settings = Settings(
        entry_sharding_enabled=True,
        config_delivery="subscription",
        entry_segments={"ru-a": [RU_A]},
        entry_sharding_base_url="https://cfg.test",
    )
    assert provisioning._config_from(data) == sharded_subscription_url(
        "https://cfg.test", NODE
    )
    provisioning.settings = Settings(config_delivery="subscription")
    assert provisioning._config_from(data) == f"https://sub.test/p/{NODE}/"


# --------------------------------------------------------------- the endpoint
def _sharded_settings(**extra: Any) -> Settings:
    values: dict[str, Any] = {
        "autoban_mode": "automatic",
        "entry_sharding_enabled": True,
        "config_delivery": "subscription",
        "entry_segments": {"ru-a": [RU_A], "ru-b": [RU_B]},
        "entry_shared_hosts": [DE],
        "subscription_fingerprint": "chrome",
    }
    return Settings(**{**values, **extra})


class Upstream:
    def __init__(self, bodies: dict[str, str], status: int = 200) -> None:
        self.bodies = bodies
        self.status = status
        self.agents: list[str] = []

    async def __call__(self, node: str, agent: str) -> tuple[int, str, dict[str, str]]:
        self.agents.append(agent)
        body = self.bodies.get(agent, self.bodies.get("*", ""))
        headers = {
            "Subscription-Userinfo": "upload=0; download=0; total=0; expire=0",
            "Profile-Update-Interval": "12",
            "Set-Cookie": "panel=1",
            "Profile-Web-Page-Url": "https://sub.test/p/" + node + "/",
        }
        return self.status, body, headers


async def _client(services: Any) -> TestClient:
    from api import create_api_app

    return TestClient(TestServer(create_api_app(services)))


async def test_endpoint_is_off_by_default(services: Any) -> None:
    async with await _client(services) as client:
        response = await client.get(f"/sub/{NODE}/")
        assert response.status == 404


async def test_endpoint_serves_only_the_users_segment(services: Any) -> None:
    services.settings = _sharded_settings()
    await _user(services.db, 42)
    body = "\n".join([_vless(RU_A, fp="firefox"), _vless(RU_B), _vless(DE)])
    upstream = Upstream({"*": base64.b64encode(body.encode()).decode()})
    services.hiddify.fetch_subscription = upstream
    segment = assign_segment(
        42,
        ["ru-a", "ru-b"],
        derive_key(services.settings.jwt_secret.get_secret_value()),
    )
    hidden = RU_B if segment == "ru-a" else RU_A
    async with await _client(services) as client:
        response = await client.get(
            f"/sub/{NODE}/", headers={"User-Agent": "HiddifyNext/2"}
        )
        assert response.status == 200
        text = base64.b64decode(await response.text()).decode()
        assert hidden not in text
        assert DE in text
        assert "fp=chrome" in text
        assert "fp=firefox" not in text
        assert response.headers["Profile-Update-Interval"] == "12"
        assert "Set-Cookie" not in response.headers
        # The panel's own user page would show the unfiltered pool.
        assert "Profile-Web-Page-Url" not in response.headers
    assert upstream.agents == ["HiddifyNext/2"]
    assert (await services.db.get_entry_segment(42))["segment"] == segment


async def test_endpoint_falls_back_to_links_for_unknown_formats(services: Any) -> None:
    services.settings = _sharded_settings()
    await _user(services.db, 43)
    upstream = Upstream(
        {"ClashMeta/1": "proxies:\n  - {name: a}\n", "*": _vless(DE) + "\n"}
    )
    services.hiddify.fetch_subscription = upstream
    async with await _client(services) as client:
        response = await client.get(
            f"/sub/{NODE}", headers={"User-Agent": "ClashMeta/1"}
        )
        assert response.status == 200
        assert DE in await response.text()
    assert upstream.agents == ["ClashMeta/1", "v2rayNG/1.9.0"]


async def test_endpoint_empty_segment_keeps_the_old_profile(services: Any) -> None:
    services.settings = _sharded_settings(entry_shared_hosts=[])
    await _user(services.db, 44)
    services.hiddify.fetch_subscription = Upstream({"*": _vless("192.0.2.99")})
    async with await _client(services) as client:
        response = await client.get(f"/sub/{NODE}/")
        assert response.status == 503


async def test_endpoint_upstream_failure(services: Any) -> None:
    services.settings = _sharded_settings()
    await _user(services.db, 45)

    async def broken(node: str, agent: str) -> tuple[int, str, dict[str, str]]:
        raise HiddifyError("down")

    services.hiddify.fetch_subscription = broken
    async with await _client(services) as client:
        assert (await client.get(f"/sub/{NODE}/")).status == 502


async def test_endpoint_unknown_and_inactive_look_the_same(services: Any) -> None:
    services.settings = _sharded_settings()
    await services.db.create_user(46, "u", None)  # pending, no node
    services.hiddify.fetch_subscription = Upstream({"*": _vless(DE)})
    async with await _client(services) as client:
        assert (await client.get(f"/sub/{uuid.uuid4()}/")).status == 404
        assert (await client.get("/sub/not-a-uuid/")).status == 404
        assert (await client.get(f"/sub/{NODE.upper()}/")).status == 404


async def test_endpoint_locks_out_guessing_addresses(services: Any) -> None:
    services.settings = _sharded_settings(sub_misses_per_ip_hour=3)
    await _user(services.db, 47)
    services.hiddify.fetch_subscription = Upstream({"*": _vless(DE)})
    async with await _client(services) as client:
        for _ in range(3):
            assert (await client.get(f"/sub/{uuid.uuid4()}/")).status == 404
        # Locked out now, even with the right UUID.
        assert (await client.get(f"/sub/{NODE}/")).status == 429


async def test_endpoint_inactive_user_is_not_a_guess(services: Any) -> None:
    services.settings = _sharded_settings(sub_misses_per_ip_hour=2)
    await _user(services.db, 50, NODE)
    other = str(uuid.uuid4())
    await _user(services.db, 51, other)
    await services.db.set_subscription(
        51, status="expired", expires_at=1, node_username=other, config_link=None
    )
    services.hiddify.fetch_subscription = Upstream({"*": _vless(DE)})
    async with await _client(services) as client:
        # An ended rental's app keeps refreshing: 404, never a lockout of the
        # address (carrier NAT shares it with active users).
        for _ in range(4):
            assert (await client.get(f"/sub/{other}/")).status == 404
        assert (await client.get(f"/sub/{NODE}/")).status == 200


async def test_endpoint_per_user_rate_limit(services: Any) -> None:
    services.settings = _sharded_settings(sub_rate_per_user_hour=2)
    await _user(services.db, 48)
    services.hiddify.fetch_subscription = Upstream({"*": _vless(DE)})
    async with await _client(services) as client:
        assert (await client.get(f"/sub/{NODE}/")).status == 200
        assert (await client.get(f"/sub/{NODE}/")).status == 200
        limited = await client.get(f"/sub/{NODE}/")
        assert limited.status == 429
        assert "Retry-After" in limited.headers


async def test_endpoint_per_ip_rate_limit(services: Any) -> None:
    services.settings = _sharded_settings(sub_rate_per_ip_10min=2)
    async with await _client(services) as client:
        for _ in range(2):
            await client.get(f"/sub/{uuid.uuid4()}/")
        assert (await client.get(f"/sub/{uuid.uuid4()}/")).status == 429


async def test_endpoint_caps_distinct_sources(services: Any) -> None:
    services.settings = _sharded_settings(sub_sources_per_user_day=2)
    await _user(services.db, 49)
    services.hiddify.fetch_subscription = Upstream({"*": _vless(DE)})
    async with await _client(services) as client:
        for ip in ("192.0.2.1", "192.0.2.2"):
            response = await client.get(
                f"/sub/{NODE}/", headers={"X-Forwarded-For": ip}
            )
            assert response.status == 200
        third = await client.get(
            f"/sub/{NODE}/", headers={"X-Forwarded-For": "192.0.2.3"}
        )
        assert third.status == 429


async def test_hiddify_fetch_never_sends_the_admin_key() -> None:
    import httpx

    from services.hiddify import HiddifyClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=_vless(DE))

    client = HiddifyClient(
        "https://panel.test",
        "22222222-2222-4222-8222-222222222222",
        user_proxy_path="userpath",
        subscription_base_url="https://sub.test:8443",
        transport=httpx.MockTransport(handler),
    )
    try:
        status, body, _ = await client.fetch_subscription(NODE, "HiddifyNext/2")
    finally:
        await client.close()
    assert status == 200
    assert DE in body
    assert str(seen[0].url) == f"https://sub.test:8443/userpath/{NODE}/"
    assert "hiddify-api-key" not in {k.lower() for k in seen[0].headers}
    assert seen[0].headers["user-agent"] == "HiddifyNext/2"


# --------------------------------------------------------------- admin script
async def test_admin_script_pins_and_reports(
    tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    import importlib.util

    path = str(tmp_path / "bot.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("ENTRY_SEGMENTS", json.dumps({"ru-a": [RU_A], "ru-b": [RU_B]}))
    database = Database(path)
    await database.connect()
    await _user(database, 900)
    await database.close()

    spec = importlib.util.spec_from_file_location(
        "entry_segments_script", "scripts/entry_segments.py"
    )
    assert spec is not None
    assert spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    assert await script.run(["--show", "900"]) == 0
    assert "would get" in capsys.readouterr().out
    assert await script.run(["--set", "900", "ru-b"]) == 0
    assert "ENTRY_SEGMENTS_OK" in capsys.readouterr().out
    assert await script.run(["--set", "900", "nope"]) == 1
    assert await script.run(["--set", "901", "ru-a"]) == 1
    capsys.readouterr()
    assert await script.run([]) == 0
    report = capsys.readouterr().out
    assert "ENTRY_SHARDING: off" in report
    assert "users ru-b/admin: 1" in report
    assert NODE not in report
    assert await script.run(["--clear", "900"]) == 0
