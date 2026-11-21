"""Security boundary regressions, including the real HTTP fallback path."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote

import pytest

from services.sharding import SegmentPlan, filter_subscription
from tests.test_entry_sharding import (
    NODE,
    PLAN,
    RU_A,
    RU_B,
    Upstream,
    _client,
    _sharded_settings,
    _user,
    _vless,
)


def _xray() -> dict[str, Any]:
    return {
        "outbounds": [
            {
                "protocol": "vless",
                "tag": "allowed",
                "settings": {
                    "vnext": [{"address": RU_A, "port": 443, "users": [{"id": NODE}]}]
                },
            }
        ]
    }


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("field", ["vnext", "servers"])
def test_mixed_schema_cannot_bypass_primary_server(strict: bool, field: str) -> None:
    plan = SegmentPlan.build({"ru-a": [RU_A], "ru-b": [RU_B]}, strict=strict)
    config = {
        "outbounds": [
            {"type": "vless", "server": RU_B, "settings": {field: [{"address": RU_A}]}}
        ]
    }
    assert filter_subscription(json.dumps(config), plan, "ru-a") is None


@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("field", ["dns", "route", "routing", "endpoints", "extension"])
def test_unaccounted_profile_fields_request_safe_fallback(
    field: str, wrapped: bool
) -> None:
    config = _xray()
    config[field] = {"peers": [{"address": RU_B}], "private_key": "test-key"}
    assert (
        filter_subscription(json.dumps([config] if wrapped else config), PLAN, "ru-a")
        is None
    )


@pytest.mark.parametrize("extra", [True, False])
def test_xhttp_download_destination_is_never_forwarded(extra: bool) -> None:
    config = _xray()
    settings = {"downloadSettings": {"address": RU_B, "port": 443, "network": "xhttp"}}
    config["outbounds"][0]["streamSettings"] = {
        "network": "xhttp",
        "xhttpSettings": {"extra": settings} if extra else settings,
    }
    assert filter_subscription(json.dumps(config), PLAN, "ru-a") is None


def test_freeform_remarks_are_replaced_with_segment_label() -> None:
    config = _xray()
    config["remarks"] = f"backup {RU_B}"
    result = filter_subscription(json.dumps(config), PLAN, "ru-a")
    assert result is not None
    assert result.kept == 1
    assert RU_B not in result.body
    assert json.loads(result.body)["remarks"] == "ru-a"


@pytest.mark.parametrize(
    "outbound",
    [
        {"type": "unknown", "server": RU_A},
        {"type": "unknown", "peers": [{"address": RU_B}]},
        {"type": "vless", "server": RU_A, "extension": {"address": RU_B}},
        {"type": "selector", "tag": "bad", "outbounds": [{}]},
        {
            "protocol": "vless",
            "server": RU_B,
            "settings": {"vnext": [{"address": RU_A}]},
        },
        "opaque-address-list",
    ],
)
def test_unknown_or_malformed_outbound_is_not_passthrough(outbound: Any) -> None:
    config = _xray()
    config["outbounds"].append(outbound)
    assert filter_subscription(json.dumps(config), PLAN, "ru-a") is None


def test_nested_groups_and_defaults_have_no_dangling_references() -> None:
    config = {
        "outbounds": [
            {"type": "vless", "tag": "a", "server": RU_A},
            {"type": "vless", "tag": "b", "server": RU_B},
            {"type": "urltest", "tag": "inner", "outbounds": ["b"]},
            {"type": "selector", "tag": "outer", "outbounds": ["inner"]},
            {
                "type": "selector",
                "tag": "select",
                "outbounds": ["a", "outer"],
                "default": "outer",
            },
        ]
    }
    result = filter_subscription(json.dumps(config), PLAN, "ru-a")
    assert result is not None
    items = json.loads(result.body)["outbounds"]
    assert {item["tag"] for item in items} == {"a", "select"}
    assert items[-1]["outbounds"] == ["a"]
    assert "default" not in items[-1]


def test_ipv6_equivalent_spellings_are_filtered() -> None:
    plan = SegmentPlan.build({"a": ["2001:db8::1"], "b": ["2001:db8::2"]})
    body = "vless://test@[2001:0db8:0:0:0:0:0:1]:443\nvless://test@[2001:0db8::2]:443"
    result = filter_subscription(body, plan, "a")
    assert result is not None
    assert (result.kept, result.removed) == (1, 1)


@pytest.mark.parametrize("wrapped", [False, True])
def test_fallback_links_cannot_smuggle_xhttp_or_query_extensions(wrapped: bool) -> None:
    extra = quote(json.dumps({"downloadSettings": {"address": RU_B}}))
    body = _vless(RU_A) + "\n" + _vless(RU_A, type="xhttp", extra=extra)
    encoded = base64.b64encode(body.encode()).decode() if wrapped else body
    result = filter_subscription(encoded, PLAN, "ru-a")
    assert result is not None
    assert (result.kept, result.removed) == (1, 1)
    text = base64.b64decode(result.body).decode() if wrapped else result.body
    assert RU_B not in text
    assert "extra=" not in text


def test_link_and_vmess_display_metadata_cannot_expose_other_entries() -> None:
    data = {"add": RU_A, "port": 443, "id": NODE, "ps": RU_B}
    vmess = "vmess://" + base64.b64encode(json.dumps(data).encode()).decode()
    result = filter_subscription(
        _vless(RU_A).split("#")[0] + "#" + RU_B + "\n" + vmess, PLAN, "ru-a"
    )
    assert result is not None
    first, second = result.body.splitlines()
    assert RU_B not in first
    assert RU_B not in base64.b64decode(second.split("://")[1]).decode()


@pytest.mark.parametrize(
    ("fallback", "status"),
    [
        (_vless(RU_A), 200),
        (_vless(RU_B), 503),
        ('{"endpoints": []}', 502),
    ],
)
async def test_http_unknown_json_retries_and_filters_links(
    services: Any, fallback: str, status: int
) -> None:
    services.settings = _sharded_settings(entry_shared_hosts=[])
    await _user(services.db, 4200)
    await services.db.set_entry_segment(4200, "ru-a", "admin")
    config = _xray()
    config["endpoints"] = [{"address": RU_B}]
    upstream = Upstream({"*": json.dumps(config), "v2rayNG/1.9.0": fallback})
    services.hiddify.fetch_subscription = upstream
    async with await _client(services) as client:
        response = await client.get(
            f"/sub/{NODE}/", headers={"User-Agent": "HiddifyNext/2"}
        )
        assert response.status == status
        assert RU_B not in await response.text()
        assert RU_B not in str(response.headers)
    assert upstream.agents == ["HiddifyNext/2", "v2rayNG/1.9.0"]


async def test_http_json_and_headers_do_not_disclose_metadata(services: Any) -> None:
    services.settings = _sharded_settings(entry_shared_hosts=[])
    await _user(services.db, 4201)
    await services.db.set_entry_segment(4201, "ru-a", "admin")
    config = _xray()
    config["remarks"] = RU_B

    async def upstream(node: str, agent: str) -> tuple[int, str, dict[str, str]]:
        return (
            200,
            json.dumps(config),
            {
                "Profile-Title": RU_B,
                "Support-Url": f"https://{RU_B}",
                "Content-Disposition": f'attachment; filename="{RU_B}"',
                "Subscription-Userinfo": RU_B,
                "Profile-Update-Interval": "12",
            },
        )

    services.hiddify.fetch_subscription = upstream
    async with await _client(services) as client:
        response = await client.get(f"/sub/{NODE}/")
        assert response.status == 200
        assert RU_B not in await response.text()
        assert RU_B not in str(response.headers)
        assert response.headers["Profile-Update-Interval"] == "12"


def test_loose_mode_does_not_preserve_unparseable_endpoints() -> None:
    plan = SegmentPlan.build({"ru-a": [RU_A]}, strict=False)
    config = _xray()
    config["outbounds"][0]["settings"]["vnext"].extend(
        [{"port": 443}, {"address": None}, {"address": ""}]
    )
    result = filter_subscription(json.dumps(config), plan, "ru-a")
    assert result is not None
    assert len(json.loads(result.body)["outbounds"][0]["settings"]["vnext"]) == 1
    result = filter_subscription("vless://", plan, "ru-a")
    assert result is not None
    assert result.kept == 0


def test_expanded_ipv6_sni_cannot_disclose_other_segment() -> None:
    plan = SegmentPlan.build({"a": [RU_A], "b": ["2001:db8::2"]})
    result = filter_subscription(_vless(RU_A, sni="2001:0db8:0:0:0:0:0:2"), plan, "a")
    assert result is not None
    assert result.kept == 0
