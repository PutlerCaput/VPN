"""Disk cleanup of a VPN server, and the memory guard of the DNS block-zone update."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLEANUP = ROOT / "scripts" / "server_disk_cleanup.sh"
DNS_UPDATE = ROOT / "scripts" / "awg_dns_update.sh"

FAKE_LOGGER = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_LOG"\nexit 0\n'


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def old(path: Path, days: int) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


@pytest.fixture
def server(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    fwupd = tmp_path / "fwupd"
    fwupd.mkdir()
    (fwupd / "firmware.cab").write_bytes(b"x" * 10)
    atop = tmp_path / "atop"
    atop.mkdir()
    (atop / "atop_old.log").write_bytes(b"old")
    old(atop / "atop_old.log", 30)
    (atop / "atop_new.log").write_bytes(b"new")
    deb = tmp_path / "cloudflared.deb"
    deb.write_bytes(b"deb")
    return {
        **os.environ,
        "FAKE_LOG": str(log),
        "APT_BIN": str(write_exe(bin_dir / "apt-get", FAKE_LOGGER)),
        "JOURNAL_BIN": str(write_exe(bin_dir / "journalctl", FAKE_LOGGER)),
        "FWUPD_DIR": str(fwupd),
        "ATOP_DIR": str(atop),
        "DEB_FILE": str(deb),
    }


def cleanup(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test script
        ["/bin/bash", str(CLEANUP), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_removes_nothing(server: dict[str, str]) -> None:
    result = cleanup(server)
    assert result.returncode == 0, result.stderr
    assert "DISK_CLEANUP_DRY_RUN" in result.stdout
    assert Path(server["FWUPD_DIR"], "firmware.cab").exists()
    assert Path(server["ATOP_DIR"], "atop_old.log").exists()
    calls = Path(server["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "apt-get clean" not in calls
    assert "vacuum" not in calls


def test_apply_removes_caches_and_old_logs_only(server: dict[str, str]) -> None:
    result = cleanup(server, "--apply")
    assert result.returncode == 0, result.stderr
    assert "DISK_CLEANUP_OK" in result.stdout
    calls = Path(server["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "apt-get clean" in calls
    assert "journalctl --vacuum-size=100M" in calls
    assert not Path(server["FWUPD_DIR"], "firmware.cab").exists()
    assert not Path(server["ATOP_DIR"], "atop_old.log").exists()
    assert Path(server["ATOP_DIR"], "atop_new.log").exists()


def test_the_installer_stays_without_the_flag(server: dict[str, str]) -> None:
    assert cleanup(server, "--apply").returncode == 0
    assert Path(server["DEB_FILE"]).exists()


def test_the_installer_goes_with_the_flag(server: dict[str, str]) -> None:
    result = cleanup(server, "--apply", "--with-deb")
    assert result.returncode == 0, result.stderr
    assert not Path(server["DEB_FILE"]).exists()


def test_unknown_argument_is_refused(server: dict[str, str]) -> None:
    result = cleanup(server, "--rm-root")
    assert result.returncode == 2
    assert "usage" in result.stderr


def dns_update(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test script
        ["/bin/bash", str(DNS_UPDATE)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def dns_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    write_exe(bin_dir / "docker", FAKE_LOGGER)
    # curl always fails: a test that reaches the download has passed the memory check.
    write_exe(bin_dir / "curl", "#!/bin/sh\nexit 22\n")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
    }


def meminfo(tmp_path: Path, available_kb: int) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(
        f"MemTotal: 2014000 kB\nMemAvailable: {available_kb} kB\n", encoding="utf-8"
    )
    return path


def test_low_memory_refuses_before_any_download(
    dns_env: dict[str, str], tmp_path: Path
) -> None:
    dns_env["MEMINFO"] = str(meminfo(tmp_path, 300 * 1024))
    result = dns_update(dns_env)
    assert result.returncode == 1
    assert "only 300 MB available" in result.stderr
    assert "previous block zone stays" in result.stderr


def test_enough_memory_gets_as_far_as_the_download(
    dns_env: dict[str, str], tmp_path: Path
) -> None:
    dns_env["MEMINFO"] = str(meminfo(tmp_path, 900 * 1024))
    result = dns_update(dns_env)
    assert result.returncode == 1
    assert "download failed" in result.stderr
    assert "memory available: 900 MB" in result.stdout


def test_the_list_selection_has_no_tif_list() -> None:
    text = DNS_UPDATE.read_text(encoding="utf-8")
    lists_block = text.split("LISTS=(", 1)[1].split(")", 1)[0]
    assert "tif-onlydomains" not in lists_block
    assert "nsfw-onlydomains" in lists_block
    assert "gambling-onlydomains" in lists_block
    assert "phishing_army" in lists_block
