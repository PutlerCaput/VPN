"""scripts/ru_firewall.sh with stand-ins for iptables, systemd and the journal.

The RU server is the VPN entry: the script may touch only the INPUT chain, it
must not be able to lock the owner out, and the database tunnel is admitted
from the DE server only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ru_firewall.sh"

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the script refuses to run without root"
)

STUBS = {
    "iptables": """#!/bin/bash
if [[ "$1" == "-S" ]]; then
  echo "-P INPUT ${FAKE_POLICY:-ACCEPT}"
  exit 0
fi
echo "iptables $*" >> "$CALLS"
[[ "$*" != *"$FAKE_FAIL_ON"* || -z "$FAKE_FAIL_ON" ]]
""",
    "iptables-save": '#!/bin/bash\necho "# ruleset at $(date +%s%N)"\n',
    "iptables-restore": '#!/bin/bash\ncat >/dev/null\necho "restore" >> "$CALLS"\n',
    "systemd-run": '#!/bin/bash\necho "systemd-run $*" >> "$CALLS"\n',
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$CALLS"\n',
    "journalctl": '#!/bin/bash\nprintf "%s" "$FAKE_JOURNAL"\n',
}
ACCEPTED = "Accepted publickey for root from 203.0.113.5 port 50000 ssh2\n"


class Box:
    def __init__(self, tmp: Path) -> None:
        self.etc = tmp / "etc"
        (self.etc / "iptables").mkdir(parents=True)
        self.rules = self.etc / "iptables" / "rules.v4"
        self.pending = self.etc / "iptables" / ".firewall-pending"
        self.calls = tmp / "calls.log"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ETC_DIR": str(self.etc),
            "CALLS": str(self.calls),
            "FAKE_JOURNAL": "",
            "FAKE_FAIL_ON": "",
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def log(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def rules_set(self) -> list[str]:
        return [line[9:] for line in self.log() if line.startswith("iptables ")]


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_report_changes_nothing(box: Box) -> None:
    result = box.run()
    assert result.returncode == 0
    assert "udp 51820 only from 89.125.210.172" in result.stdout
    assert "tcp 8444 only on wg-db from 10.204.0.2" in result.stdout
    assert result.stdout.rstrip().endswith("RU_FIREWALL_REPORT_DONE")
    assert box.log() == []


def test_apply_touches_only_input_and_flips_the_policy_last(box: Box) -> None:
    result = box.run("--apply")
    assert result.returncode == 0, result.stderr
    assert "RU_FIREWALL_APPLIED" in result.stdout
    rules = box.rules_set()
    assert rules[0] == "-F INPUT"
    assert rules[-1] == "-P INPUT DROP"
    # The session in progress is admitted before anything can be dropped.
    assert rules.index(
        "-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
    ) < rules.index("-A INPUT -m conntrack --ctstate INVALID -j DROP")
    assert "-A INPUT -p tcp --dport 22 -m conntrack --ctstate NEW -j ACCEPT" in rules
    for rule in rules:
        assert "FORWARD" not in rule
        assert "OUTPUT" not in rule
        assert "-t nat" not in rule
    # The undo is armed, and the pending file names the snapshot and the time.
    assert any(
        line.startswith("systemd-run ") and "--revert" in line for line in box.log()
    )
    snapshot, applied_at = box.pending.read_text().splitlines()
    assert Path(snapshot).is_file()
    assert applied_at.isdigit()


def test_the_database_tunnel_is_admitted_from_de_only(box: Box) -> None:
    box.run("--apply")
    rules = box.rules_set()
    assert "-A INPUT -p udp -s 89.125.210.172 --dport 51820 -j ACCEPT" in rules
    assert (
        "-A INPUT -i wg-db -p tcp -s 10.204.0.2 --dport 8444 "
        "-m conntrack --ctstate NEW -j ACCEPT"
    ) in rules
    # Neither port is opened to everyone.
    assert not any("--dport 8444" in rule and "-i wg-db" not in rule for rule in rules)
    assert not any("--dport 51820" in rule and "-s " not in rule for rule in rules)


def test_the_tunnel_rules_can_be_left_out(box: Box) -> None:
    box.run("--apply", DB_TUNNEL_PEER="", VPN_TUNNEL_PEER="")
    assert not any("udp" in rule or "wg-db" in rule for rule in box.rules_set())
    report = box.run(DB_TUNNEL_PEER="", VPN_TUNNEL_PEER="").stdout
    assert "Database tunnel" not in report
    assert "VPN tunnel" not in report


def test_the_vpn_tunnel_is_admitted_from_de_only(box: Box) -> None:
    assert "VPN tunnel: udp 51821 only from 89.125.210.172" in box.run().stdout
    box.run("--apply")
    rules = box.rules_set()
    assert "-A INPUT -p udp -s 89.125.210.172 --dport 51821 -j ACCEPT" in rules
    assert not any("--dport 51821" in rule and "-s " not in rule for rule in rules)
    assert rules[-1] == "-P INPUT DROP"


@pytest.mark.parametrize(
    "bad",
    [
        {"DB_TUNNEL_PEER": "1.2.3.4;reboot"},
        {"DB_TUNNEL_PEER": "example.com"},
        {"DB_GATEWAY_PEER": "10.204.0.2 -j ACCEPT"},
        {"DB_TUNNEL_IF": "wg db"},
        {"DB_GATEWAY_PORT": "8444a"},
        {"DB_TUNNEL_PORT": "$(id)"},
        {"VPN_TUNNEL_PEER": "1.2.3.4 -j ACCEPT"},
        {"VPN_TUNNEL_PORT": "51821;id"},
    ],
)
def test_settings_that_are_not_plain_values_are_refused(
    box: Box, bad: dict[str, str]
) -> None:
    result = box.run("--apply", **bad)
    assert result.returncode == 1
    assert "RU_FIREWALL_STOPPED" in result.stderr
    assert box.log() == []
    assert not box.pending.exists()


def test_a_failed_rule_restores_the_snapshot(box: Box) -> None:
    result = box.run("--apply", FAKE_FAIL_ON="--dport 80")
    assert result.returncode == 1
    assert "snapshot was restored" in result.stderr
    assert "restore" in box.log()
    assert not box.pending.exists()
    assert "iptables -P INPUT DROP" not in box.log()


def test_confirm_needs_a_login_made_after_the_change(box: Box) -> None:
    box.run("--apply")
    refused = box.run("--confirm")
    assert refused.returncode == 1
    assert "NEW ssh login" in refused.stderr
    assert box.pending.exists()
    assert not box.rules.exists()
    kept = box.run("--confirm", FAKE_JOURNAL=ACCEPTED)
    assert kept.returncode == 0, kept.stderr
    assert "RU_FIREWALL_OK" in kept.stdout
    assert box.rules.read_text().startswith("# ruleset")
    assert not box.pending.exists()
    assert "systemctl stop ru-firewall-revert.timer" in box.log()


def test_revert_while_pending_restores_everything(box: Box) -> None:
    box.run("--apply")
    result = box.run("--revert")
    assert result.returncode == 0
    assert "restored the ruleset from before --apply" in result.stdout
    assert box.log()[-2:] == ["restore", "systemctl stop ru-firewall-revert.timer"]
    assert not box.pending.exists()
    assert not box.rules.exists()


def test_revert_after_confirm_opens_input_and_saves(box: Box) -> None:
    box.run("--apply")
    box.run("--confirm", FAKE_JOURNAL=ACCEPTED)
    box.calls.unlink()
    result = box.run("--revert")
    assert result.returncode == 0
    assert box.rules_set() == ["-P INPUT ACCEPT", "-F INPUT"]
    assert "INPUT is open again" in result.stdout


def test_a_second_apply_waits_for_the_first(box: Box) -> None:
    box.run("--apply")
    again = box.run("--apply")
    assert again.returncode == 1
    assert "already waiting" in again.stderr


def test_status(box: Box) -> None:
    assert box.run("--status").stdout.startswith(
        "WARN firewall: INPUT policy is not DROP"
    )
    assert box.run("--status", FAKE_POLICY="DROP").stdout.startswith("OK firewall")
    box.run("--apply")
    assert "waits for --confirm" in box.run("--status", FAKE_POLICY="DROP").stdout
