"""Offline subscription policy contract for four independent entry routes."""

from __future__ import annotations

import pytest

from deploy.four_node.plan_subscriptions import subscription_plan
from services.sharding import SegmentPlan, filter_subscription

INVENTORY = {
    "ru1": "1.1.1.1",
    "ru2": "8.8.8.8",
    "nl1": "9.9.9.9",
    "nl2": "4.2.2.2",
}


def test_all_routes_policy_defaults_to_no_sharding() -> None:
    plan = subscription_plan(INVENTORY)
    assert plan["config_delivery"] == "subscription"
    assert plan["entry_sharding_enabled"] is False
    assert plan["requires_per_user_xray_provisioning"] is True
    assert plan["client_auto_failover_verified"] is False
    assert [route["host"] for route in plan["expected_routes"]] == [
        INVENTORY[k] for k in ("ru1", "ru2", "nl1", "nl2")
    ]


def test_isolated_pairs_never_expose_other_pair() -> None:
    plan = subscription_plan(INVENTORY, policy="isolated-pairs")
    assert plan["entry_sharding_enabled"] is True
    assert plan["entry_shared_hosts"] == []
    segments = SegmentPlan.build(plan["entry_segments"])
    links = "\n".join(
        f"vless://00000000-0000-4000-8000-000000000001@{host}:443"
        "?security=reality"
        for host in INVENTORY.values()
    )
    first = filter_subscription(links, segments, "ru-one")
    second = filter_subscription(links, segments, "ru-two")
    assert first is not None
    assert second is not None
    assert first.kept == second.kept == 2
    assert INVENTORY["ru2"] not in first.body
    assert INVENTORY["nl2"] not in first.body
    assert INVENTORY["ru1"] not in second.body
    assert INVENTORY["nl1"] not in second.body


def test_unknown_policy_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported subscription policy"):
        subscription_plan(INVENTORY, policy="auto-magic")
