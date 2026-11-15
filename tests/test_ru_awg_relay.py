"""scripts/ru_awg_relay.sh: UDP forwarding of AmneziaWG through the RU server."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ru_awg_relay.sh"

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


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
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
        "SS_BIN": str(write_exe(tmp_path / "ss", "#!/bin/sh\nexit 0\n")),
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
    f"nat|PREROUTING|-p udp --dport 39051 -j DNAT --to-destination {DE}:39051",
    f"nat|POSTROUTING|-d {DE} -p udp --dport 39051 -j MASQUERADE",
    f"filter|FORWARD|-d {DE} -p udp --dport 39051 -j ACCEPT",
    f"filter|FORWARD|-s {DE} -p udp --sport 39051 -j ACCEPT",
]


def test_apply_adds_the_four_rules_and_saves(env: dict[str, str]) -> None:
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert sorted(rules(env)) == sorted(EXPECTED)
    assert "RU_AWG_RELAY_OK" in result.stdout
    assert "4 rules added" in result.stdout
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    # FORWARD accepts go first, ahead of any DROP that may follow.
    assert f"-t filter -I FORWARD 1 -d {DE} -p udp --dport 39051 -j ACCEPT" in log
    assert (Path(env["FAKE_STATE"]).parent / "saved").is_file()


def test_apply_twice_adds_nothing_the_second_time(env: dict[str, str]) -> None:
    assert run(env, "--apply").returncode == 0
    second = run(env, "--apply")
    assert second.returncode == 0, second.stderr
    assert "0 rules added" in second.stdout
    assert len(rules(env)) == len(EXPECTED)


def test_apply_refuses_without_ip_forward(env: dict[str, str], tmp_path: Path) -> None:
    env["SYSCTL_BIN"] = str(write_exe(tmp_path / "sysctl0", "#!/bin/sh\necho 0\n"))
    result = run(env, "--apply")
    assert result.returncode == 1
    assert "ip_forward" in result.stderr
    assert rules(env) == []


def test_apply_refuses_a_port_used_on_this_server(
    env: dict[str, str], tmp_path: Path
) -> None:
    busy = "#!/bin/sh\necho 'UNCONN 0 0 0.0.0.0:39051 0.0.0.0:*'\n"
    env["SS_BIN"] = str(write_exe(tmp_path / "ss_busy", busy))
    result = run(env, "--apply")
    assert result.returncode == 1
    assert "already used" in result.stderr
    assert rules(env) == []


def test_check_changes_nothing(env: dict[str, str]) -> None:
    result = run(env, "--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("missing:") == len(EXPECTED)
    assert rules(env) == []


def test_remove_takes_every_rule_away(env: dict[str, str]) -> None:
    assert run(env, "--apply").returncode == 0
    result = run(env, "--remove")
    assert result.returncode == 0, result.stderr
    assert rules(env) == []
    assert "4 rules removed" in result.stdout


def test_another_port_and_a_bad_port(env: dict[str, str]) -> None:
    assert run(env, "--apply", "40000").returncode == 0
    assert any("--dport 40000" in rule for rule in rules(env))
    bad = run(env, "--apply", "80")
    assert bad.returncode == 1
    assert "1024-65535" in bad.stderr
