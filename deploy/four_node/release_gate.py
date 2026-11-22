"""Offline, fail-closed release gate for the four-node commercial VPN topology.

This module performs NO network requests and NEVER provisions a server.
Evidence is self-attested by an operator and must be backed by private logs
from real client tests. It is not an independent security certification.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = 2
MAX_AGE = timedelta(days=7)
FUTURE_TOLERANCE = timedelta(minutes=5)

GLOBAL_CHECKS = (
    "per_user_credentials_and_revocation_verified",
    "aggregate_quota_on_both_exits_verified",
    "revocation_during_node_partition_verified",
    "independent_egress_alerts_verified",
    "subscription_routes_verified",
    "client_fallback_verified",
    "billing_webhooks_and_reconciliation_verified",
    "private_keys_rotated_and_stored_safely",
    "encrypted_offsite_backup_verified",
    "isolated_restore_verified",
    "rollback_tested",
    "recovery_mode_and_source_verified",
    "single_writer_and_payment_cutover_verified",
    "post_write_rollback_tested",
    "raw_subscription_bypass_closed",
    "legal_and_provider_terms_reviewed",
)

NODE_CHECKS = (
    "ssh_host_key_verified",
    "firewall_and_listeners_reviewed",
    "rollback_access_verified",
)

EXIT_CHECKS = (
    "xray_config_test_passed",
    "reality_tls_target_tested",
    "per_user_client_handshake_verified",
)

INGRESS_CHECKS = (
    "haproxy_syntax_test_passed",
    "tcp_passthrough_to_correct_exit_verified",
    "client_tunnel_from_ru_network_verified",
)

NODES = ("ru1", "ru2", "nl1", "nl2")
REPO_ROOT = Path(__file__).resolve().parents[2]


def required_evidence() -> dict[str, object]:
    """An all-false template makes accidental 'ready' impossible."""
    return {
        "schema": SCHEMA,
        "checked_at": None,
        "global": dict.fromkeys(GLOBAL_CHECKS, False),
        "nodes": {
            name: dict.fromkeys(
                NODE_CHECKS
                + (EXIT_CHECKS if name.startswith("nl") else INGRESS_CHECKS),
                False,
            )
            for name in NODES
        },
    }


def _checked_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    return None


def evaluate(
    evidence: object, *, now: datetime | None = None
) -> tuple[bool, list[str]]:
    """Return (ready, missing checks), printing no values from the evidence."""
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
        return False, ["schema"]
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    now = now.astimezone(UTC)
    checked = _checked_at(evidence.get("checked_at"))
    issues: list[str] = []
    if checked is None or not -FUTURE_TOLERANCE <= now - checked <= MAX_AGE:
        issues.append("checked_at")

    global_results = evidence.get("global")
    if not isinstance(global_results, dict):
        global_results = {}
    for check in GLOBAL_CHECKS:
        if global_results.get(check) is not True:
            issues.append(f"global.{check}")

    nodes = evidence.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
    for node in NODES:
        values = nodes.get(node)
        if not isinstance(values, dict):
            values = {}
        fields = NODE_CHECKS + (
            EXIT_CHECKS if node.startswith("nl") else INGRESS_CHECKS
        )
        for check in fields:
            if values.get(check) is not True:
                issues.append(f"nodes.{node}.{check}")
    return not issues, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--print-template",
        action="store_true",
        help="Print safe, all-false JSON template to stdout",
    )
    args = parser.parse_args(argv)
    if args.print_template:
        print(json.dumps(required_evidence(), indent=2))
        return 0
    if args.evidence is None:
        parser.error("--evidence or --print-template is required")
    source = args.evidence.resolve()
    if source == REPO_ROOT or REPO_ROOT in source.parents:
        parser.exit(2, "ERROR: evidence must stay outside the public Git repo\n")
    try:
        payload: Any = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Path, file content and malformed JSON never go to CI or public logs.
        parser.exit(2, "ERROR: cannot read private evidence JSON\n")
    ready, missing = evaluate(payload)
    print("READY" if ready else "BLOCKED")
    for check in missing:
        print("MISSING", check)
    print("Evidence is operator-reported; live client tests are required.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
