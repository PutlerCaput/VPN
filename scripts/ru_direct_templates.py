#!/usr/bin/env python3
"""Hiddify Manager subscription templates that send Russian sites past the server.

Traffic that has reached the server leaves it with the server's foreign address,
so only the client app can open Russian sites directly. Hiddify Manager can hand that
rule to the app inside the subscription: v2rayNG then receives full Xray configs
(USE_CUSTOM_JSON_FOR_V2RAYNG) and the sing-box apps a sing-box config, each built
from a template. These functions take Hiddify Manager's own default templates, read from
its container on the server, and add the rules. Nothing else in them changes.

The rules use no geosite list: a tag missing from the geo file of an older app
stops its core from starting. They name the .ru, .su and .рф zones and Russian
IP addresses (geoip:ru, present in every geoip file). Russian names are looked
up at Yandex DNS, directly, so the answer fits a Russian connection.

  python3 ru_direct_templates.py v2ray < default.json > ru-direct.json
  python3 ru_direct_templates.py singbox < default.json > ru-direct.json
  python3 ru_direct_templates.py check < ru-direct.json > check.json  # xray -test
"""

from __future__ import annotations

import copy
import json
import sys
from typing import Any

# Xray domain rules: the zones themselves and every name under them. ".рф" is
# written in its DNS form, as names reach the core.
RU_DOMAINS = ["domain:ru", "domain:su", "domain:xn--p1ai"]
RU_SUFFIXES = ["ru", "su", "xn--p1ai"]
RU_DNS = "77.88.8.8"  # Yandex DNS
DIRECT = "direct"
MARK = "ru-direct"  # names the rules this module adds, so a rerun adds none


def v2ray_template(default: dict[str, Any]) -> dict[str, Any]:
    """Hiddify Manager's v2ray-json template with Russian sites going direct."""
    template = copy.deepcopy(default)
    for inbound in template.get("inbounds", []):
        # The app connects to these ports on the device itself; nobody else on
        # the same Wi-Fi should be able to use them.
        inbound["listen"] = "127.0.0.1"
    outbounds = template.setdefault("outbounds", [])
    tags = {outbound.get("tag") for outbound in outbounds}
    # Hiddify Manager puts the server ("proxy") first: anything unmatched goes there.
    if DIRECT not in tags:
        outbounds.append(
            {
                "tag": DIRECT,
                "protocol": "freedom",
                "settings": {"domainStrategy": "UseIP"},
            }
        )
    if "block" not in tags:
        outbounds.append({"tag": "block", "protocol": "blackhole"})
    routing = template.setdefault("routing", {})
    # Names that match no rule are looked up, so a Russian address goes direct
    # whatever its name.
    routing["domainStrategy"] = "IPIfNonMatch"
    rules = [r for r in routing.get("rules", []) if not _ours(r)]
    routing["rules"] = [
        {
            "ruleTag": f"{MARK}-lan",
            "type": "field",
            "ip": ["geoip:private"],
            "outboundTag": DIRECT,
        },
        {
            "ruleTag": f"{MARK}-domains",
            "type": "field",
            "domain": RU_DOMAINS,
            "outboundTag": DIRECT,
        },
        {
            "ruleTag": f"{MARK}-ip",
            "type": "field",
            "ip": ["geoip:ru"],
            "outboundTag": DIRECT,
        },
        *rules,
    ]
    dns = template.setdefault("dns", {})
    servers = [s for s in dns.get("servers", []) if not _ours_dns(s)]
    dns["servers"] = [
        {"address": RU_DNS, "domains": RU_DOMAINS, "skipFallback": True},
        *servers,
    ]
    return template


def singbox_template(default: dict[str, Any]) -> dict[str, Any]:
    """Hiddify Manager's sing-box template with Russian sites going direct."""
    template = copy.deepcopy(default)
    outbounds = template.setdefault("outbounds", [])
    if DIRECT not in {o.get("tag") for o in outbounds}:
        outbounds.append({"type": "direct", "tag": DIRECT})
    route = template.setdefault("route", {})
    rules = [r for r in route.get("rules", []) if not _ours(r)]
    ours = {"domain_suffix": RU_SUFFIXES, "outbound": DIRECT}
    # After the rules that catch DNS and the local network, before the rest.
    position = 0
    while position < len(rules) and (
        "protocol" in rules[position] or "ip_is_private" in rules[position]
    ):
        position += 1
    rules.insert(position, ours)
    route["rules"] = rules
    dns = template.get("dns")
    if isinstance(dns, dict):
        local = _local_dns_tag(dns)
        if local is not None:
            dns_rules = [r for r in dns.get("rules", []) if not _ours(r)]
            dns["rules"] = [{"domain_suffix": RU_SUFFIXES, "server": local}, *dns_rules]
    return template


def check_config(template: dict[str, Any]) -> dict[str, Any]:
    """The template as Hiddify Manager serves it, with a stand-in server, for xray -test."""
    config = copy.deepcopy(template)
    config.pop("remarks", None)
    server = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "127.0.0.1",
                    "port": 9,
                    "users": [
                        {
                            "id": "00000000-0000-4000-8000-000000000000",
                            "encryption": "none",
                        }
                    ],
                }
            ]
        },
    }
    config["outbounds"] = [server, *config.get("outbounds", [])]
    return config


def _ours(rule: Any) -> bool:
    if not isinstance(rule, dict):
        return False
    if str(rule.get("ruleTag", "")).startswith(MARK):
        return True
    return rule.get("domain_suffix") == RU_SUFFIXES


def _ours_dns(server: Any) -> bool:
    return isinstance(server, dict) and server.get("domains") == RU_DOMAINS


def _local_dns_tag(dns: dict[str, Any]) -> str | None:
    for server in dns.get("servers", []):
        if isinstance(server, dict) and (
            server.get("address") == "local" or server.get("detour") == DIRECT
        ):
            tag = server.get("tag")
            return str(tag) if tag else None
    return None


def main(argv: list[str]) -> int:
    kinds = {
        "v2ray": v2ray_template,
        "singbox": singbox_template,
        "check": check_config,
    }
    if len(argv) != 2 or argv[1] not in kinds:
        print(
            "usage: ru_direct_templates.py v2ray|singbox|check < in.json > out.json",
            file=sys.stderr,
        )
        return 2
    try:
        source = json.load(sys.stdin)
    except ValueError as exc:
        print(f"not a JSON template: {exc}", file=sys.stderr)
        return 1
    if not isinstance(source, dict):
        print("not a JSON template: expected an object", file=sys.stderr)
        return 1
    json.dump(kinds[argv[1]](source), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
