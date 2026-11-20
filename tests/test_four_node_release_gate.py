"""Release readiness must fail closed without operator-confirmed evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from deploy.four_node.release_gate import (
    GLOBAL_CHECKS,
    NODES,
    evaluate,
    required_evidence,
)

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)


def approved() -> dict[str, object]:
    evidence = required_evidence()
    evidence["checked_at"] = NOW.isoformat()
    evidence["global"] = dict.fromkeys(GLOBAL_CHECKS, True)
    nodes = evidence["nodes"]
    assert isinstance(nodes, dict)
    for node in NODES:
        checks = nodes[node]
        assert isinstance(checks, dict)
        nodes[node] = dict.fromkeys(checks, True)
    return evidence


def test_blank_gate_is_blocked() -> None:
    ok, missing = evaluate(required_evidence(), now=NOW)
    assert not ok
    assert "checked_at" in missing
    assert "global.isolated_restore_verified" in missing
    assert "nodes.ru1.client_tunnel_from_ru_network_verified" in missing
    assert "nodes.nl2.per_user_client_handshake_verified" in missing


def test_only_all_checked_evidence_can_pass() -> None:
    ok, missing = evaluate(approved(), now=NOW)
    assert ok
    assert missing == []


def test_gate_rejects_aged_or_future_evidence() -> None:
    for timepoint in (NOW - timedelta(days=8), NOW + timedelta(hours=1)):
        evidence = approved()
        evidence["checked_at"] = timepoint.isoformat()
        ok, missing = evaluate(evidence, now=NOW)
        assert not ok
        assert missing == ["checked_at"]


def test_gate_refuses_strings_and_false_proofs() -> None:
    evidence = approved()
    global_checks = evidence["global"]
    assert isinstance(global_checks, dict)
    global_checks["isolated_restore_verified"] = "true"
    ok, missing = evaluate(evidence, now=NOW)
    assert not ok
    assert "global.isolated_restore_verified" in missing


def test_gate_refuses_missing_nodes() -> None:
    evidence = approved()
    nodes = evidence["nodes"]
    assert isinstance(nodes, dict)
    nodes.pop("nl1")
    ok, missing = evaluate(evidence, now=NOW)
    assert not ok
    assert "nodes.nl1.ssh_host_key_verified" in missing


def test_gate_refuses_unversioned_and_naive_evidence() -> None:
    evidence = approved()
    evidence["schema"] = 999
    assert evaluate(evidence, now=NOW) == (False, ["schema"])
    evidence = approved()
    evidence["checked_at"] = "2026-11-20T14:00:00"
    ok, missing = evaluate(evidence, now=NOW)
    assert not ok
    assert "checked_at" in missing
