"""Backup status must never claim offsite custody without dispatch evidence."""

from __future__ import annotations

import os
import subprocess  # nosec B404
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup_status.sh"


def status(tmp_path: Path, send: int, sent_marker: bool) -> str:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "backup-test.tar.gz.age").write_bytes(b"encrypted-placeholder")
    if sent_marker:
        (backup_dir / ".last-sent").touch()
    config = tmp_path / "backup.conf"
    config.write_text(f"BACKUP_DIR={backup_dir}\nSEND_TELEGRAM={send}\n")
    env = {**os.environ, "BACKUP_ENV": str(config)}
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["/bin/bash", str(SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("sent_marker", [False, True])
def test_disabled_dispatch_cannot_claim_offsite(sent_marker: bool, tmp_path: Path) -> None:
    result = status(tmp_path, 0, sent_marker)
    assert result.startswith("WARN")
    assert "offsite dispatch is disabled" in result


def test_missing_send_marker_is_a_warning(tmp_path: Path) -> None:
    result = status(tmp_path, 1, False)
    assert result.startswith("WARN")


def test_fresh_marker_does_not_prove_delivery_or_restore(tmp_path: Path) -> None:
    result = status(tmp_path, 1, True)
    assert result.startswith("OK local encrypted backup")
    assert "send recorded" in result
    assert "restore not verified" in result
