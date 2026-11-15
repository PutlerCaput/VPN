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
EXPECTED = [
    f"nat|PREROUTING|-p tcp --dport 443 -j DNAT --to-destination {DE}:2096",
    f"nat|PREROUTING|-p tcp --dport 2087 -j DNAT --to-destination {DE}:2096",
    f"nat|PREROUTING|-p tcp --dport 8443 -j DNAT --to-destination {DE}:2096",
    f"nat|PREROUTING|-p tcp --dport 2083 -j DNAT --to-destination {DE}:2098",
    f"nat|POSTROUTING|-d {DE} -p tcp --dport 2096 -j MASQUERADE",
    f"nat|POSTROUTING|-d {DE} -p tcp --dport 2098 -j MASQUERADE",
    "mangle|FORWARD|-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu",
]
ADDED = [
    f"-t nat -A PREROUTING -p tcp --dport 443 -j DNAT --to-destination {DE}:2096",
    f"-t nat -A PREROUTING -p tcp --dport 2087 -j DNAT --to-destination {DE}:2096",
    f"-t nat -A PREROUTING -p tcp --dport 8443 -j DNAT --to-destination {DE}:2096",
    f"-t nat -A PREROUTING -p tcp --dport 2083 -j DNAT --to-destination {DE}:2098",
    f"-t nat -A POSTROUTING -d {DE} -p tcp --dport 2096 -j MASQUERADE",
    f"-t nat -A POSTROUTING -d {DE} -p tcp --dport 2098 -j MASQUERADE",
    "-t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu",
]


def test_apply_adds_the_seven_rules_and_saves(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert rules(env) == EXPECTED
    assert "RU_VLESS_RELAY_OK" in result.stdout
    assert "7 rules added" in result.stdout
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    for needle in ADDED:
        assert needle in log
    assert "-t filter" not in log
    assert (tmp_path / "saved").is_file()


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
    assert "7 rules removed" in result.stdout
    assert "RU_VLESS_RELAY_REMOVED" in result.stdout


def test_remove_clears_duplicate_copies(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["FAKE_STATE"]).write_text(
        json.dumps(EXPECTED + EXPECTED), encoding="utf-8"
    )
    result = run(env, "--remove")
    assert result.returncode == 0, result.stderr
    assert rules(env) == []
    assert "14 rules removed" in result.stdout


def test_another_de_address_and_a_bad_one(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["DE_IP"] = "203.0.113.10"
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert any("203.0.113.10:2096" in rule for rule in rules(env))
    assert any("203.0.113.10:2098" in rule for rule in rules(env))
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
