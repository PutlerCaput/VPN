"""scripts/ru_vless_relay.sh: TCP forwarding of VLESS through the RU server."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ru_vless_relay.sh"

FAKE_IPTABLES = r"""#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_STATE"]
with open(state_path) as handle:
    rules = json.load(handle)
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
table = args[1]
op, chain = args[2], args[3]
rest = args[4:]
if op == "-I" and rest and rest[0].isdigit():
    rest = rest[1:]
key = f"{table}|{chain}|{' '.join(rest)}"
if op == "-C":
    sys.exit(0 if key in rules else 1)
if op in ("-A", "-I"):
    rules.append(key)
elif op == "-D":
    if os.environ.get("FAKE_FAIL_DELETE"):
        sys.exit(1)
    rules.remove(key)
with open(state_path, "w") as handle:
    json.dump(rules, handle)
"""


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _env(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "rules.json"
    state.write_text("[]", encoding="utf-8")
    log = tmp_path / "iptables.log"
    log.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "FAKE_STATE": str(state),
        "FAKE_LOG": str(log),
        "IPTABLES_BIN": str(write_exe(tmp_path / "iptables", FAKE_IPTABLES)),
        "SYSCTL_BIN": str(write_exe(tmp_path / "sysctl", "#!/bin/sh\necho 1\n")),
        "SAVE_BIN": str(
            write_exe(tmp_path / "save", f"#!/bin/sh\necho saved >> {tmp_path}/saved\n")
        ),
        "IPTABLES_SAVE_BIN": str(
            write_exe(tmp_path / "iptables-save", f"#!/bin/sh\ncat {state}\n")
        ),
        "BACKUP_DIR": str(tmp_path / "backup"),
        "IP_BIN": str(write_exe(tmp_path / "ip", "#!/bin/sh\nexit 0\n")),
        "RELAY_SYSCTL_CONF": str(tmp_path / "sysctl.d" / "91-vpn-relay.conf"),
        "RELAY_MODULES_CONF": str(tmp_path / "modules-load.d" / "vpn-relay.conf"),
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test script
        ["/bin/bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def rules(env: dict[str, str]) -> list[str]:
    return list(json.loads(Path(env["FAKE_STATE"]).read_text(encoding="utf-8")))


DE = "89.125.210.172"
TUN = "10.205.0.2"  # DE inside the wg-vpn tunnel
EXPECTED = [
    f"nat|PREROUTING|-p tcp --dport 443 -j DNAT --to-destination {TUN}:2443",
    f"nat|PREROUTING|-p tcp --dport 2087 -j DNAT --to-destination {TUN}:2443",
    f"nat|PREROUTING|-p tcp --dport 8443 -j DNAT --to-destination {TUN}:2443",
    f"nat|PREROUTING|-p tcp --dport 2083 -j DNAT --to-destination {TUN}:2443",
    f"nat|PREROUTING|-p tcp --dport 2085 -j DNAT --to-destination {TUN}:2443",
    f"nat|POSTROUTING|-d {TUN} -o wg-vpn -p tcp --dport 2443 -j MASQUERADE",
    "mangle|FORWARD|-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu",
    f"nat|PREROUTING|-p udp --dport 36099 -j DNAT --to-destination {DE}:36099",
    f"nat|POSTROUTING|-d {DE} -p udp --dport 36099 -j MASQUERADE",
    f"nat|PREROUTING|-p udp --dport 443 -j DNAT --to-destination {DE}:36099",
]
ADDED = [
    f"-t nat -A PREROUTING -p tcp --dport 443 -j DNAT --to-destination {TUN}:2443",
    f"-t nat -A PREROUTING -p tcp --dport 2087 -j DNAT --to-destination {TUN}:2443",
    f"-t nat -A PREROUTING -p tcp --dport 8443 -j DNAT --to-destination {TUN}:2443",
    f"-t nat -A PREROUTING -p tcp --dport 2083 -j DNAT --to-destination {TUN}:2443",
    f"-t nat -A PREROUTING -p tcp --dport 2085 -j DNAT --to-destination {TUN}:2443",
    f"-t nat -A POSTROUTING -d {TUN} -o wg-vpn -p tcp --dport 2443 -j MASQUERADE",
    "-t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu",
    f"-t nat -A PREROUTING -p udp --dport 36099 -j DNAT --to-destination {DE}:36099",
    f"-t nat -A POSTROUTING -d {DE} -p udp --dport 36099 -j MASQUERADE",
    f"-t nat -A PREROUTING -p udp --dport 443 -j DNAT --to-destination {DE}:36099",
]
# The Marzban forwards live on RU before the move to Hiddify (16.11.2026).
OBSOLETE = [
    f"nat|PREROUTING|-p tcp --dport 443 -j DNAT --to-destination {DE}:2096",
    f"nat|PREROUTING|-p tcp --dport 2087 -j DNAT --to-destination {DE}:2096",
    f"nat|PREROUTING|-p tcp --dport 8443 -j DNAT --to-destination {DE}:2096",
    f"nat|PREROUTING|-p tcp --dport 2083 -j DNAT --to-destination {DE}:2098",
    f"nat|POSTROUTING|-d {DE} -p tcp --dport 2096 -j MASQUERADE",
    f"nat|POSTROUTING|-d {DE} -o eth0 -p tcp --dport 2096 -j MASQUERADE",
    f"nat|POSTROUTING|-d {DE} -p tcp --dport 2098 -j MASQUERADE",
]
# Direct TCP forwards over the open internet, live on RU before wg-vpn (16.11.2026).
DIRECT = [
    f"nat|PREROUTING|-p tcp --dport 443 -j DNAT --to-destination {DE}:2443",
    f"nat|PREROUTING|-p tcp --dport 2087 -j DNAT --to-destination {DE}:2443",
    f"nat|PREROUTING|-p tcp --dport 8443 -j DNAT --to-destination {DE}:2443",
    f"nat|PREROUTING|-p tcp --dport 2083 -j DNAT --to-destination {DE}:2443",
    f"nat|PREROUTING|-p tcp --dport 2085 -j DNAT --to-destination {DE}:2443",
    f"nat|POSTROUTING|-d {DE} -p tcp --dport 2443 -j MASQUERADE",
]
# AmneziaWG goes through RU too; this script must never touch it.
AWG = [
    f"nat|PREROUTING|-p udp --dport 39051 -j DNAT --to-destination {DE}:39051",
    f"nat|PREROUTING|-p udp --dport 44877 -j DNAT --to-destination {DE}:44877",
    f"nat|POSTROUTING|-d {DE} -p udp --dport 39051 -j MASQUERADE",
    f"nat|POSTROUTING|-d {DE} -p udp --dport 44877 -j MASQUERADE",
]


def backups(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "backup").glob("rules.v4.before-ru-vless-relay-*"))


def test_apply_adds_the_ten_rules_and_saves(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert rules(env) == EXPECTED
    assert "RU_VLESS_RELAY_OK" in result.stdout
    assert "10 rules added" in result.stdout
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    for needle in ADDED:
        assert needle in log
    assert "-t filter" not in log
    assert (tmp_path / "saved").is_file()
    assert len(backups(tmp_path)) == 1


def test_apply_twice_adds_nothing_the_second_time(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert run(env, "--apply").returncode == 0
    second = run(env, "--apply")
    assert second.returncode == 0, second.stderr
    assert "0 rules added" in second.stdout
    assert rules(env) == EXPECTED


def test_apply_refuses_without_ip_forward(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["SYSCTL_BIN"] = str(write_exe(tmp_path / "sysctl0", "#!/bin/sh\necho 0\n"))
    result = run(env, "--apply")
    assert result.returncode == 1
    assert "RU_VLESS_RELAY_STOPPED" in result.stderr
    assert "ip_forward" in result.stderr
    assert rules(env) == []


def test_apply_stops_when_save_fails(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["SAVE_BIN"] = str(write_exe(tmp_path / "save_fail", "#!/bin/sh\nexit 1\n"))
    result = run(env, "--apply")
    assert result.returncode == 1
    assert "not saved" in result.stderr
    assert rules(env) == EXPECTED


def test_check_changes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = run(env, "--check")
    assert result.returncode == 0, result.stderr
    assert "ip_forward: 1" in result.stdout
    assert result.stdout.count("missing:") == len(EXPECTED)
    assert "RU_VLESS_RELAY_CHECK_OK" in result.stdout
    assert rules(env) == []
    assert not (tmp_path / "saved").is_file()


def test_check_reports_forwarding_off(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["SYSCTL_BIN"] = str(write_exe(tmp_path / "sysctl0", "#!/bin/sh\necho 0\n"))
    result = run(env, "--check")
    assert result.returncode == 0, result.stderr
    assert "ip_forward: 0" in result.stdout
    assert rules(env) == []


def test_remove_takes_every_rule_away(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert run(env, "--apply").returncode == 0
    result = run(env, "--remove")
    assert result.returncode == 0, result.stderr
    assert rules(env) == []
    assert "10 rules removed" in result.stdout
    assert "RU_VLESS_RELAY_REMOVED" in result.stdout


def test_remove_clears_duplicate_copies(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["FAKE_STATE"]).write_text(
        json.dumps(EXPECTED + EXPECTED), encoding="utf-8"
    )
    result = run(env, "--remove")
    assert result.returncode == 0, result.stderr
    assert rules(env) == []
    assert "20 rules removed" in result.stdout


def test_another_de_address_and_a_bad_one(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["DE_IP"] = "203.0.113.10"
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    udp = [rule for rule in rules(env) if "nat|" in rule and "-p udp" in rule]
    assert udp
    assert all("203.0.113.10" in rule for rule in udp)
    assert sum(f"{TUN}:2443" in rule for rule in rules(env)) == 5
    env["DE_IP"] = "not-an-ip"
    bad = run(env, "--check")
    assert bad.returncode == 1
    assert "IPv4" in bad.stderr


def test_usage(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = run(env)
    assert result.returncode == 1
    assert "usage" in result.stderr
    extra = run(env, "--apply", "443")
    assert extra.returncode == 1
    assert rules(env) == []


def test_apply_replaces_marzban_forwards_keeps_amneziawg(tmp_path: Path) -> None:
    env = _env(tmp_path)
    mss = next(rule for rule in EXPECTED if rule.startswith("mangle|"))
    Path(env["FAKE_STATE"]).write_text(
        json.dumps(OBSOLETE + AWG + [mss]), encoding="utf-8"
    )
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert sorted(rules(env)) == sorted(AWG + EXPECTED)
    assert "9 rules added, 7 obsolete removed" in result.stdout
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8").splitlines()
    first_delete = next(i for i, line in enumerate(log) if " -D " in line)
    last_add = max(i for i, line in enumerate(log) if " -A " in line)
    assert last_add < first_delete, "new forwards go in before the old ones go"
    changes = [line for line in log if " -D " in line or " -A " in line]
    assert not any("39051" in line or "44877" in line for line in changes)
    backup = backups(tmp_path)
    assert len(backup) == 1
    assert "2096" in backup[0].read_text(encoding="utf-8")


def test_apply_with_nothing_to_do_makes_no_backup(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["FAKE_STATE"]).write_text(json.dumps(AWG + EXPECTED), encoding="utf-8")
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert "0 rules added, 0 obsolete removed" in result.stdout
    assert backups(tmp_path) == []
    assert sorted(rules(env)) == sorted(AWG + EXPECTED)


def test_check_lists_obsolete_rules(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["FAKE_STATE"]).write_text(json.dumps(OBSOLETE + AWG), encoding="utf-8")
    result = run(env, "--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("obsolete:") == len(OBSOLETE)
    assert result.stdout.count("missing:") == len(EXPECTED)
    assert "39051" not in result.stdout
    assert sorted(rules(env)) == sorted(OBSOLETE + AWG)
    assert backups(tmp_path) == []


def test_remove_also_drops_obsolete_and_keeps_amneziawg(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["FAKE_STATE"]).write_text(
        json.dumps(EXPECTED + OBSOLETE + AWG), encoding="utf-8"
    )
    result = run(env, "--remove")
    assert result.returncode == 0, result.stderr
    assert sorted(rules(env)) == sorted(AWG)
    assert "17 rules removed" in result.stdout
    assert len(backups(tmp_path)) == 1


def test_apply_stops_when_the_backup_fails(tmp_path: Path) -> None:
    env = _env(tmp_path)
    failing = write_exe(tmp_path / "save_x", "#!/bin/sh\nexit 1\n")
    env["IPTABLES_SAVE_BIN"] = str(failing)
    result = run(env, "--apply")
    assert result.returncode == 1
    assert "back up" in result.stderr
    assert rules(env) == []


def test_bad_de_port(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for bad in ("0", "70000", "abc", "08x", "2096", "2098"):
        env["DE_PORT"] = bad
        result = run(env, "--check")
        assert result.returncode == 1, bad
        assert "DE_PORT" in result.stderr


def test_a_failed_delete_stops_before_saving(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["FAKE_STATE"]).write_text(json.dumps(OBSOLETE + AWG), encoding="utf-8")
    env["FAKE_FAIL_DELETE"] = "1"
    for mode in ("--apply", "--remove"):
        result = run(env, mode)
        assert result.returncode != 0, mode
        assert "could not be removed" in result.stderr, mode
        assert "RU_VLESS_RELAY_OK" not in result.stdout
        assert "RU_VLESS_RELAY_REMOVED" not in result.stdout
        assert not (tmp_path / "saved").is_file(), mode


def test_bad_hysteria2_port(tmp_path: Path) -> None:
    for bad in ("0", "70000", "x", "39051", "44877"):
        env = _env(tmp_path)
        env["HY2_PORT"] = bad
        result = run(env, "--apply")
        assert result.returncode == 1
        assert "HY2_PORT" in result.stderr
        assert rules(env) == []


def test_other_hysteria2_port(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["HY2_PORT"] = "40001"
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    dnat = f"nat|PREROUTING|-p udp --dport 40001 -j DNAT --to-destination {DE}:40001"
    assert dnat in rules(env)
    assert "UDP 40001" in result.stdout


def test_apply_moves_direct_forwards_into_the_tunnel(tmp_path: Path) -> None:
    env = _env(tmp_path)
    kept = [rule for rule in EXPECTED if rule.startswith("mangle|") or "udp" in rule]
    Path(env["FAKE_STATE"]).write_text(
        json.dumps(DIRECT + AWG + kept), encoding="utf-8"
    )
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert sorted(rules(env)) == sorted(AWG + EXPECTED)
    assert "6 rules added, 6 obsolete removed" in result.stdout
    assert "via wg-vpn" in result.stdout
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8").splitlines()
    first_delete = next(i for i, line in enumerate(log) if " -D " in line)
    last_add = max(i for i, line in enumerate(log) if " -A " in line)
    assert last_add < first_delete, "tunnel forwards go in before the direct ones go"
    changes = [line for line in log if " -D " in line or " -A " in line]
    untouched = ("39051", "44877", "36099")
    assert not any(port in line for line in changes for port in untouched)


def test_apply_stops_without_the_tunnel_interface(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["FAKE_STATE"]).write_text(json.dumps(DIRECT + AWG), encoding="utf-8")
    env["IP_BIN"] = str(write_exe(tmp_path / "ip_none", "#!/bin/sh\nexit 1\n"))
    result = run(env, "--apply")
    assert result.returncode == 1
    assert "wg-vpn" in result.stderr
    assert sorted(rules(env)) == sorted(DIRECT + AWG)
    assert backups(tmp_path) == []
    assert not (tmp_path / "saved").is_file()


def test_tunnel_address_settings(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["DE_TUNNEL_IP"] = "10.9.9.2"
    env["TUNNEL_IF"] = "wg-x"
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    masq = "nat|POSTROUTING|-d 10.9.9.2 -o wg-x -p tcp --dport 2443 -j MASQUERADE"
    assert masq in rules(env)
    for name, value, needle in (
        ("DE_TUNNEL_IP", DE, "DE_TUNNEL_IP"),
        ("DE_TUNNEL_IP", "nope", "DE_TUNNEL_IP"),
        ("TUNNEL_IF", "bad if;", "TUNNEL_IF"),
    ):
        bad_env = _env(tmp_path)
        bad_env[name] = value
        bad = run(bad_env, "--check")
        assert bad.returncode == 1, (name, value)
        assert needle in bad.stderr


def test_spare_hysteria2_port_and_conntrack(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert "UDP 36099 and 443" in result.stdout
    conf = Path(env["RELAY_SYSCTL_CONF"])
    assert "net.netfilter.nf_conntrack_max = 65536" in conf.read_text(encoding="utf-8")
    assert "conntrack max 65536" in result.stdout
    # The module loads before systemd-sysctl, so the limit survives a reboot.
    modules = Path(env["RELAY_MODULES_CONF"]).read_text(encoding="utf-8")
    assert "nf_conntrack" in modules.splitlines()
    before = Path(env["RELAY_MODULES_CONF"]).stat().st_mtime_ns
    assert run(env, "--apply").returncode == 0
    assert Path(env["RELAY_MODULES_CONF"]).stat().st_mtime_ns == before


def test_spare_port_can_be_left_out(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["HY2_BACKUP_PORT"] = ""
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert not any("--dport 443 -j DNAT" in r and "udp" in r for r in rules(env))
    assert len(rules(env)) == len(EXPECTED) - 1


def test_bad_spare_ports_and_conntrack(tmp_path: Path) -> None:
    for name, bad in (
        ("HY2_BACKUP_PORT", "39051"),
        ("HY2_BACKUP_PORT", "44877"),
        ("HY2_BACKUP_PORT", "36099"),
        ("HY2_BACKUP_PORT", "x"),
        ("HY2_BACKUP_PORT", "70000"),
        ("CONNTRACK_MAX", "12;id"),
        ("CONNTRACK_MAX", "1"),
    ):
        env = _env(tmp_path)
        env[name] = bad
        result = run(env, "--apply")
        assert result.returncode == 1, (name, bad)
        assert name in result.stderr
        assert rules(env) == []
