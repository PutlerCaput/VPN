"""Plan four-route subscription delivery without publishing customer credentials.

This is a READ-ONLY, offline planning tool. It does not configure Hiddify,
provision Xray clients, update env files, accept payments or contact servers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from deploy.four_node.generate import REPO_ROOT, load_inventory


def subscription_plan(
    inventory: dict[str, str], *, policy: str = "all-routes"
) -> dict[str, Any]:
    """Generate an explicit routing policy for the existing Hiddify bot.

    Returning these settings does NOT prove that any corresponding VLESS link
    exists upstream or that each NL exit has a registered per-customer UUID.
    """
    routes = [
        {"name": name.upper(), "host": inventory[name], "port": 443}
        for name in ("ru1", "ru2", "nl1", "nl2")
    ]
    base: dict[str, Any] = {
        "config_delivery": "subscription",
        "expected_routes": routes,
        "requires_per_user_xray_provisioning": True,
        "client_auto_failover_verified": False,
    }
    if policy == "all-routes":
        base.update(
            {
                "entry_sharding_enabled": False,
                "entry_segments": {},
                "entry_shared_hosts": [],
                "safety_note": (
                    "Client receives all four addresses only IF each is present "
                    "in its Hiddify subscription; auto-failover is not guaranteed."
                ),
            }
        )
    elif policy == "isolated-pairs":
        base.update(
            {
                "entry_sharding_enabled": True,
                "entry_segments": {
                    "ru-one": [inventory["ru1"], inventory["nl1"]],
                    "ru-two": [inventory["ru2"], inventory["nl2"]],
                },
                "entry_shared_hosts": [],
                "safety_note": (
                    "Each customer sees only one RU+NL pair. The other pair "
                    "cannot serve as an automatic backup for that customer."
                ),
            }
        )
    else:
        raise ValueError("Unsupported subscription policy")
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--policy", choices=("all-routes", "isolated-pairs"), default="all-routes"
    )
    args = parser.parse_args(argv)
    path = args.inventory.resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        parser.exit(2, "ERROR: inventory must remain outside the public Git repo\n")
    try:
        inventory = load_inventory(path)
    except (ValueError, OSError):
        parser.exit(2, "ERROR: invalid private inventory; details hidden\n")
    print(json.dumps(subscription_plan(inventory, policy=args.policy), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
