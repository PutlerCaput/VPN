"""scripts/de_lockdown.sh with stand-ins for ufw, sshd and systemd.

Prepared, not applied by any deploy: the report changes nothing, --apply arms
its own undo before touching ufw and opens every service port (AmneziaWG,
Hysteria2, the RU tunnel) before the default policy flips to deny.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "de_lockdown.sh"

STUBS = {
    "ufw": '#!/bin/bash\necho "ufw $*" >> "$CALLS"\n'
    '[[ "$1" == status ]] && echo "Status: ${FAKE_UFW:-inactive}"\n'
    '[[ -z "$FAKE_FAIL_ON" || "$*" != *"$FAKE_FAIL_ON"* ]]\n',
    "sshd": '#!/bin/bash\necho "port 2222"\n',
    "ss": '#!/bin/bash\necho "tcp 0.0.0.0:2443"\necho "udp 0.0.0.0:39051"\n',
    "systemd-run": '#!/bin/bash\necho "systemd-run $*" >> "$CALLS"\n',
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$CALLS"\n',
    "docker": "#!/bin/bash\nexit 0\n",
}


def _box(tmp: Path) -> dict[str, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp / "etc" / "ufw").mkdir(parents=True)
    (tmp / "etc" / "ufw" / "user.rules").write_text("# before\n")
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ETC_DIR": str(tmp / "etc"),
        "STATE_DIR": str(tmp / "state"),
        "CALLS": str(tmp / "calls.log"),
        "AS_ROOT_CHECK": "0",
        "FAKE_FAIL_ON": "",
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(env: dict[str, str]) -> list[str]:
    path = Path(env["CALLS"])
    return path.read_text().splitlines() if path.exists() else []


def test_report_changes_nothing(tmp_path: Path) -> None:
    env = _box(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "DE_LOCKDOWN_PLAN" in result.stdout
    for rule in (
        "ufw allow 2222/tcp",
        "ufw allow 39051/udp",
        "ufw allow 44877/udp",
        "ufw allow 36099/udp",
        "ufw allow 2443/tcp",
        "ufw allow 8443/tcp",
        "ufw allow proto udp from 45.138.157.65 to any",
        "ufw default allow routed",
        "ufw allow in on wg-vpn",
    ):
        assert rule in result.stdout
    assert all(c.startswith("ufw status") for c in _calls(env))


def test_apply_arms_undo_and_opens_ports_before_deny(tmp_path: Path) -> None:
    env = _box(tmp_path)
    result = _run(env, "--apply")
    assert result.returncode == 0, result.stderr
    calls = _calls(env)
    arm = next(i for i, c in enumerate(calls) if c.startswith("systemd-run"))
    ssh = calls.index("ufw allow 2222/tcp comment ssh")
    deny = calls.index("ufw default deny incoming")
    first_rule = next(i for i, c in enumerate(calls) if c.startswith("ufw allow"))
    assert arm < first_rule <= ssh < deny
    assert all(calls.index(c) < deny for c in calls if "39051" in c or "44877" in c)
    assert calls[-1] == "ufw --force enable"
    # A second apply is refused while the first is pending.
    assert _run(env, "--apply").returncode == 1


def test_revert_restores_the_backup(tmp_path: Path) -> None:
    env = _box(tmp_path)
    assert _run(env, "--apply").returncode == 0
    rules = tmp_path / "etc" / "ufw" / "user.rules"
    rules.write_text("# changed\n")
    result = _run(env, "--revert")
    assert result.returncode == 0, result.stderr
    assert rules.read_text() == "# before\n"
    assert "ufw --force disable" in _calls(env)  # ufw was inactive before
    assert _run(env, "--confirm").returncode == 1  # nothing pending any more


def test_failed_rule_reverts(tmp_path: Path) -> None:
    env = _box(tmp_path)
    env["FAKE_FAIL_ON"] = "36099"
    result = _run(env, "--apply")
    assert result.returncode == 1
    assert "reverted" in result.stderr
    assert "ufw default deny incoming" not in _calls(env)
    assert not (tmp_path / "state" / "pending").exists()


def test_second_ru_entry(tmp_path: Path) -> None:
    env = _box(tmp_path)
    env["RU_PEER"] = "45.138.157.65 198.51.100.20"
    env["TUNNEL_IF"] = "wg-vpn wg-vpn2"
    out = _run(env).stdout
    assert "ufw allow proto udp from 198.51.100.20 to any" in out
    assert "ufw allow in on wg-vpn2" in out


def test_rejects_bad_values(tmp_path: Path) -> None:
    env = _box(tmp_path)
    env["ADMIN_IPS"] = "1.2.3.4; rm -rf /"
    assert _run(env).returncode == 1
    env["ADMIN_IPS"] = ""
    env["ALLOW_TCP"] = "99999"
    assert _run(env).returncode == 1
    env["ALLOW_TCP"] = "443"
    env["TUNNEL_IF"] = "wg-vpn;reboot"
    assert _run(env).returncode == 1
