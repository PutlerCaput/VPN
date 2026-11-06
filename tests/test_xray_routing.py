"""Node routing template: AUP block rules merge idempotently into Xray config."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

XRAY_DIR = Path(__file__).resolve().parent.parent / "deploy" / "xray"
spec = importlib.util.spec_from_file_location(
    "apply_routing", XRAY_DIR / "apply_routing.py"
)
assert spec is not None
assert spec.loader is not None
apply_routing: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_routing)


def template() -> dict[str, Any]:
    return json.loads((XRAY_DIR / "routing-rules.json").read_text(encoding="utf-8"))


def test_template_blocks_required_categories() -> None:
    domains = [d for r in template()["routing"]["rules"] for d in r.get("domain", [])]
    for tag in (
        "geosite:category-porn",
        "geosite:category-betting-ru",
        "ext:security.dat:malware",
        "ext:security.dat:phishing",
    ):
        assert tag in domains
    for rule in template()["routing"]["rules"]:
        assert rule["outboundTag"] == (
            "DIRECT" if rule["ruleTag"] == "owner-platform-exceptions" else "BLOCK"
        )


def test_merge_is_idempotent_and_keeps_existing_rules() -> None:
    config: dict[str, Any] = {
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
        "routing": {
            "rules": [{"type": "field", "outboundTag": "DIRECT", "ip": ["1.1.1.1"]}]
        },
    }
    apply_routing.merge(config, template(), ["domain:casino.example"])
    apply_routing.merge(config, template(), ["domain:casino.example"])
    rules = config["routing"]["rules"]
    tags = [r.get("ruleTag") for r in rules]
    assert tags == [
        "aup-malicious-domains",
        "aup-ips",
        "protect-private",
        "owner-platform-exceptions",
        "aup-domains",
        "aup-bittorrent",
        None,
    ]
    assert (
        next(r for r in rules if r.get("ruleTag") == "aup-domains")["domain"].count(
            "domain:casino.example"
        )
        == 1
    )
    assert [o["tag"] for o in config["outbounds"]] == ["DIRECT", "BLOCK"]
    assert config["routing"]["domainStrategy"] == "IPIfNonMatch"


def test_merge_into_config_without_direct_keeps_freedom_first() -> None:
    config: dict[str, Any] = {"outbounds": []}
    apply_routing.merge(config, template(), [])
    assert config["outbounds"][0]["protocol"] == "freedom"


def test_custom_blocklist_rejects_bad_entries(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text(
        "# c\n\ndomain:bad.example\nfull:www.bad.example\n", encoding="utf-8"
    )
    assert apply_routing.load_custom_domains(good) == [
        "domain:bad.example",
        "full:www.bad.example",
    ]
    bad = tmp_path / "bad.txt"
    bad.write_text('domain:x.example", "regexp:.*\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        apply_routing.load_custom_domains(bad)


def test_platform_exceptions_cannot_precede_malware_or_private_networks() -> None:
    config: dict[str, Any] = {}
    allowed = apply_routing.load_custom_domains(XRAY_DIR / "platform-allowlist.txt")
    assert "domain:youtube.com" in allowed
    assert "domain:steampowered.com" in allowed
    apply_routing.merge(config, template(), ["domain:custom.example"], allowed)
    apply_routing.merge(config, template(), ["domain:custom.example"], allowed)
    rules = config["routing"]["rules"]
    tags = [r["ruleTag"] for r in rules]
    index = tags.index("owner-platform-exceptions")
    for tag in ("aup-malicious-domains", "aup-ips", "protect-private"):
        assert tags.index(tag) < index
    assert index < tags.index("aup-domains")
    assert rules[index]["domain"] == allowed
