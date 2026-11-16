"""scripts/hiddify_cron_guard.sh: Hiddify's nightly auto-update stays commented
out, also after Hiddify's own reinstall at boot writes it back.

The script runs for real in a sandbox (ETC_DIR), so it needs no root."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hiddify_cron_guard.sh"
ACTIVE = (
    "0 3 * * * root /opt/hiddify-manager/scripts/common/../update.sh"
    " --no-gui --no-log\n"
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "ETC_DIR": str(tmp_path / "etc"),
        "BACKUP_DIR": str(tmp_path / "backup"),
        "INSTALL_PATH": str(tmp_path / "sbin" / "hiddify-cron-guard"),
    }


def _cron(tmp_path: Path) -> Path:
    path = tmp_path / "etc" / "cron.d" / "hiddify_auto_update"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test script
        ["/bin/bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_an_active_line_is_commented_out_with_a_backup(tmp_path: Path) -> None:
    env = _env(tmp_path)
    cron = _cron(tmp_path)
    cron.write_text("# Hiddify\n\n" + ACTIVE, encoding="utf-8")
    result = run(env, "--run")
    assert result.returncode == 0, result.stderr
    assert "HIDDIFY_CRON_GUARD_FIXED" in result.stdout
    lines = cron.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# Hiddify", ""]
    assert lines[2].startswith("# disabled ")
    assert lines[2].endswith(ACTIVE.strip())
    [backup] = list((tmp_path / "backup").iterdir())
    assert backup.read_text(encoding="utf-8").endswith(ACTIVE)


def test_an_already_disabled_file_is_left_untouched(tmp_path: Path) -> None:
    env = _env(tmp_path)
    cron = _cron(tmp_path)
    cron.write_text("# disabled by hand: " + ACTIVE, encoding="utf-8")
    before = cron.stat().st_mtime_ns
    result = run(env, "--run")
    assert result.returncode == 0
    assert "already off" in result.stdout
    assert cron.stat().st_mtime_ns == before
    assert not (tmp_path / "backup").exists()


def test_a_missing_file_is_fine(tmp_path: Path) -> None:
    result = run(_env(tmp_path), "--run")
    assert result.returncode == 0
    assert "does not exist" in result.stdout


def test_install_writes_path_and_timer_units_and_fixes_at_once(tmp_path: Path) -> None:
    env = _env(tmp_path)
    cron = _cron(tmp_path)
    cron.write_text(ACTIVE, encoding="utf-8")
    result = run(env, "--install")
    assert result.returncode == 0, result.stderr
    units = tmp_path / "etc" / "systemd" / "system"
    path_unit = (units / "hiddify-cron-guard.path").read_text(encoding="utf-8")
    timer = (units / "hiddify-cron-guard.timer").read_text(encoding="utf-8")
    service = (units / "hiddify-cron-guard.service").read_text(encoding="utf-8")
    assert f"PathChanged={cron}" in path_unit
    assert "OnBootSec=5min" in timer
    assert "OnUnitActiveSec=15min" in timer
    # The units run a copy outside the checkout.
    assert f"ExecStart=/bin/bash {env['INSTALL_PATH']} --run" in service
    assert Path(env["INSTALL_PATH"]).read_bytes() == SCRIPT.read_bytes()
    assert not any(
        line.strip() and not line.startswith("#")
        for line in cron.read_text(encoding="utf-8").splitlines()
    )


def test_status_for_the_doctor(tmp_path: Path) -> None:
    env = _env(tmp_path)
    cron = _cron(tmp_path)
    cron.write_text(ACTIVE, encoding="utf-8")
    assert run(env, "--status").stdout.startswith(
        "WARN Hiddify nightly auto-update is ACTIVE"
    )
    run(env, "--run")
    assert "nothing keeps it off after a reboot" in run(env, "--status").stdout
    run(env, "--install")
    assert run(env, "--status").stdout.startswith("OK ")
    # Hiddify's reinstall at boot writes the line back: the doctor notices.
    cron.write_text(ACTIVE, encoding="utf-8")
    assert run(env, "--status").stdout.startswith("WARN")
    run(env, "--remove")
    assert not (
        tmp_path / "etc" / "systemd" / "system" / "hiddify-cron-guard.path"
    ).exists()


def test_bad_usage(tmp_path: Path) -> None:
    result = run(_env(tmp_path), "--nope")
    assert result.returncode == 1
    assert "usage" in result.stderr


def test_the_doctor_reads_the_guard() -> None:
    text = (ROOT / "scripts" / "update_bot.sh").read_text(encoding="utf-8")
    assert 'hiddify_cron_guard.sh" --status' in text
