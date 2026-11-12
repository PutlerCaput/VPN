"""scripts/ru_integrity.sh with stand-ins for AIDE, rkhunter, dpkg, audit and systemd.

The script only reports. What matters is that its one-line answer is right:
quiet when nothing changed, and naming each kind of change when something did.
(It was also run against the real AIDE 0.18 and rkhunter 1.4 by hand.)
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ru_integrity.sh"

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the script refuses to run without root"
)

# aide: the "database" is a listing of the watched files with their checksums.
AIDE = r"""#!/bin/bash
conf="${1#--config=}"
out="$(sed -n 's/^database_out=file://p' "$conf")"
db="$(sed -n 's/^database_in=file://p' "$conf")"
listing() {
  local skip
  skip="$(sed -n 's/^!\(.*\)\$$/\1/p' "$conf" | paste -sd'|' -)"
  sed -n 's/^\(\/.*\) Watched$/\1/p' "$conf" | while read -r folder; do
    find "$folder" -type f -exec sha256sum {} +
  done | { if [[ -n "$skip" ]]; then grep -Ev "  ($skip)$"; else cat; fi; } | sort -k2
}
case "$2" in
  --config-check) [[ "$FAKE_AIDE_REJECTS" != 1 ]] ;;
  --init) listing > "$out" ;;
  --check)
    [[ "$FAKE_AIDE_BROKEN" == 1 ]] && exit 17
    now="$(listing)"
    was="$(cut -c67- "$db" | sort)"
    is="$(cut -c67- <<<"$now" | sort)"
    added=$(comm -13 <(echo "$was") <(echo "$is") | grep -c .)
    removed=$(comm -23 <(echo "$was") <(echo "$is") | grep -c .)
    changed=$(comm -13 <(sort "$db") <(sort <<<"$now") | grep -c .)
    changed=$((changed - added))
    printf 'Summary:\n  Total number of entries:\t9\n  Added entries:\t\t%s\n' "$added"
    printf '  Removed entries:\t\t%s\n  Changed entries:\t\t%s\n' "$removed" "$changed"
    exit $(( (added > 0) + 2 * (removed > 0) + 4 * (changed > 0) )) ;;
esac
"""
RKHUNTER = """#!/bin/bash
echo "rkhunter $*" >> "$CALLS"
[[ "$1" == "--check" ]] && cat "$FAKE_RKHUNTER" 2>/dev/null
exit 0
"""
STUBS = {
    "aide": AIDE,
    "rkhunter": RKHUNTER,
    "dpkg": '#!/bin/bash\n[[ "$1" == "-V" ]] && cat "$FAKE_DPKG" 2>/dev/null\nexit 0\n',
    "auditctl": "#!/bin/bash\nexit 0\n",
    "augenrules": '#!/bin/bash\necho "augenrules $*" >> "$CALLS"\n',
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$CALLS"\n',
    "sshd": '#!/bin/bash\necho "permitrootlogin without-password"\n',
    "apt-get": '#!/bin/bash\necho "apt-get $*" >> "$CALLS"\n',
}


class Box:
    def __init__(self, tmp: Path) -> None:
        self.prefix = tmp / "root"
        self.watched = tmp / "server"
        (self.watched / "etc").mkdir(parents=True)
        (self.watched / "bin").mkdir()
        (self.watched / "etc" / "sshd_config").write_text("PermitRootLogin no\n")
        (self.watched / "etc" / "mtab").write_text("changes by itself\n")
        (self.watched / "bin" / "sshd").write_text("binary\n")
        (self.prefix / "var" / "lib" / "dpkg").mkdir(parents=True)
        self.dpkg_status = self.prefix / "var" / "lib" / "dpkg" / "status"
        self.dpkg_status.write_text("")
        self.calls = tmp / "calls.log"
        self.rkhunter = tmp / "rkhunter.txt"
        self.rkhunter.write_text("Warning: Hidden directory found: /etc/.java\n")
        self.dpkg = tmp / "dpkg.txt"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PREFIX": str(self.prefix),
            "WATCH": f"{self.watched / 'etc'} {self.watched / 'bin'} /nonexistent",
            "SKIP": str(self.watched / "etc" / "mtab"),
            "CALLS": str(self.calls),
            "FAKE_RKHUNTER": str(self.rkhunter),
            "FAKE_DPKG": str(self.dpkg),
        }
        self.logs = self.prefix / "var" / "log" / "vpn-integrity"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def status(self) -> str:
        return self.run("--status").stdout.strip()

    def log(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_report_changes_nothing(box: Box) -> None:
    result = box.run()
    assert result.returncode == 0
    assert "baseline: none yet" in result.stdout
    assert "WARN integrity: never checked" in result.stdout
    assert result.stdout.rstrip().endswith("RU_INTEGRITY_REPORT_DONE")
    assert not (box.prefix / "etc").exists()
    assert box.run("--check").returncode == 1
    assert box.run("--accept").returncode == 1


def test_apply_takes_a_baseline_and_schedules_the_weekly_check(box: Box) -> None:
    result = box.run("--apply")
    assert result.returncode == 0, result.stderr
    assert "RU_INTEGRITY_ON" in result.stdout
    # The warning rkhunter has about the server as it is today is not news.
    assert (
        "1 warnings about the server as it is today are taken as known" in result.stdout
    )
    assert box.status() == "OK integrity: checked 0 days ago, nothing changed"

    conf = (box.prefix / "etc" / "vpn-integrity" / "aide.conf").read_text()
    assert f"{box.watched / 'etc'} Watched" in conf
    assert "/nonexistent" not in conf
    assert f"!{box.watched / 'etc' / 'mtab'}$" in conf
    database = box.prefix / "var" / "lib" / "vpn-integrity" / "aide.db"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert (
        "ALLOW_SSH_ROOT_USER=without-password"
        in (box.prefix / "etc" / "rkhunter.conf.local").read_text()
    )
    rules = (box.prefix / "etc/audit/rules.d/vpn-integrity.rules").read_text()
    assert "-w /etc/passwd -p wa -k vpn-integrity" in rules
    timer = (box.prefix / "etc/systemd/system/vpn-integrity-check.timer").read_text()
    assert "OnCalendar=Sun" in timer
    service = (
        box.prefix / "etc/systemd/system/vpn-integrity-check.service"
    ).read_text()
    installed = box.prefix / "usr" / "local" / "sbin" / "ru_integrity.sh"
    assert f"ExecStart=/bin/bash {installed} --check" in service
    assert installed.read_text() == SCRIPT.read_text()
    log = box.log()
    assert "systemctl enable --now vpn-integrity-check.timer" in log
    # One check with one list: the distribution's own daily run is switched off.
    assert "systemctl disable --now dailyaidecheck.timer" in log
    # Nothing was missing, so nothing was installed; and never a mail server.
    assert not any(line.startswith("apt-get") for line in log)
    assert "--no-install-recommends" in SCRIPT.read_text()


def test_each_kind_of_change_is_named(box: Box) -> None:
    box.run("--apply")
    (box.watched / "etc" / "mtab").write_text("changed by itself again\n")
    assert box.status().startswith("OK integrity")
    assert box.run("--check").stdout.startswith("OK integrity")

    (box.watched / "etc" / "sshd_config").write_text("PermitRootLogin yes\n")
    (box.watched / "bin" / "planted").write_text("new\n")
    changed = box.run("--check").stdout
    assert changed.startswith("WARN integrity: 2 files changed since the baseline")
    assert "run --accept" in changed
    assert "packages were updated" not in changed

    box.dpkg.write_text("??5??????   /usr/bin/ssh\n??5?????? c /etc/ssh/sshd_config\n")
    box.rkhunter.write_text(
        "Warning: Hidden directory found: /etc/.java\n"
        "Warning: Suspect file found: /srv/.x\n"
    )
    worse = box.run("--check").stdout
    assert "1 system files differ from their packages" in worse
    assert "rkhunter has 1 new warnings" in worse
    assert (box.logs / "dpkg.txt").read_text().count("\n") == 1
    assert "/srv/.x" in (box.logs / "rkhunter.txt").read_text()
    assert ".java" not in (box.logs / "rkhunter.txt").read_text()


def test_files_the_image_leaves_out_are_not_counted_as_changes(box: Box) -> None:
    box.run("--apply")
    # Ubuntu's image leaves man pages, docs and translations out on purpose:
    # dpkg -V calls each of them "missing". Thousands of those are normal.
    box.dpkg.write_text(
        "".join(f"missing   /usr/share/man/man1/tool{n}.1.gz\n" for n in range(500))
    )
    assert box.run("--check").stdout.startswith("OK integrity")
    summary = (box.logs / "last-check.txt").read_text()
    assert "dpkg_missing=500" in summary
    assert "dpkg_modified=0" in summary
    # One real change among them is still a warning, and the only one named.
    box.dpkg.write_text("missing   /usr/share/man/x.1.gz\n??5??????   /usr/bin/ssh\n")
    changed = box.run("--check").stdout
    assert "1 system files differ from their packages" in changed
    assert "/usr/bin/ssh" in (box.logs / "dpkg.txt").read_text()
    assert "missing" not in (box.logs / "dpkg.txt").read_text()


def test_an_update_is_recognised_and_accept_makes_it_the_baseline(box: Box) -> None:
    box.run("--apply")
    (box.watched / "bin" / "sshd").write_text("new binary from an update\n")
    future = time.time() + 5
    os.utime(box.dpkg_status, (future, future))
    assert "(packages were updated since then)" in box.run("--check").stdout
    accepted = box.run("--accept")
    assert accepted.returncode == 0, accepted.stderr
    assert "RU_INTEGRITY_ACCEPTED" in accepted.stdout
    assert box.status().startswith("OK integrity")


def test_a_stale_check_is_a_warning(box: Box) -> None:
    box.run("--apply")
    summary = box.logs / "last-check.txt"
    old = int(time.time()) - 12 * 86400
    lines = [
        f"checked_at={old}" if line.startswith("checked_at=") else line
        for line in summary.read_text().splitlines()
    ]
    summary.write_text("\n".join(lines) + "\n")
    assert box.status() == "WARN integrity: last check 12 days ago"


def test_failures_stop_loudly(box: Box) -> None:
    rejected = box.run("--apply", FAKE_AIDE_REJECTS="1")
    assert rejected.returncode == 1
    assert "rejected by aide itself" in rejected.stderr
    assert not (box.prefix / "etc/systemd/system/vpn-integrity-check.timer").exists()
    box.run("--apply")
    broken = box.run("--check", FAKE_AIDE_BROKEN="1")
    assert broken.returncode == 1
    assert "could not run its check" in broken.stderr
    assert box.run("--nope").returncode == 1
