"""scripts/ssh_hardening.sh with stand-ins for sshd, systemctl and the journal.

The point of the script is that it cannot lock the owner out: it refuses when
key login was never seen working, it removes its own file when sshd does not
honour it, and a timer takes the change back unless a NEW key login confirms it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ssh_hardening.sh"

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the script refuses to run without root"
)

# sshd -T / sshd -t over the files in ETC_DIR/ssh: the first value of a keyword
# wins, drop-ins in name order before the main file, as in OpenSSH.
FAKE_SSHD = r"""#!/usr/bin/env python3
import glob
import os
import sys

etc = os.environ["ETC_DIR"]
kbd = (
    "challengeresponseauthentication"
    if os.environ.get("FAKE_OLD_SSHD") == "1"
    else "kbdinteractiveauthentication"
)
known = {
    "passwordauthentication", kbd, "permitrootlogin", "permitemptypasswords",
    "x11forwarding", "pubkeyauthentication", "include", "match", "usepam",
}
defaults = {
    "passwordauthentication": "yes",
    kbd: "no",
    "permitrootlogin": "without-password",
    "permitemptypasswords": "no",
    "x11forwarding": "yes",
    "pubkeyauthentication": "yes",
}
for pair in filter(None, os.environ.get("FAKE_DEFAULTS", "").split(",")):
    key, _, value = pair.partition("=")
    defaults[key] = value
dropin = f"{etc}/ssh/sshd_config.d/00-cyberdefency-hardening.conf"
rejects = os.environ.get("FAKE_T_FAILS") == "1" and os.path.exists(dropin)
if sys.argv[1] == "-t" and rejects:
    print("sshd: rejected", file=sys.stderr)
    sys.exit(255)
values = {}
files = sorted(glob.glob(f"{etc}/ssh/sshd_config.d/*.conf"))
files.append(f"{etc}/ssh/sshd_config")
for path in files:
    if not os.path.exists(path):
        continue
    for raw in open(path, encoding="utf-8"):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        word, _, rest = text.partition(" ")
        word = word.lower()
        if word not in known:
            print(f"Bad configuration option: {word}", file=sys.stderr)
            sys.exit(255)
        values.setdefault(word, rest.strip())
if sys.argv[1] == "-t":
    sys.exit(0)
for key, value in defaults.items():
    values.setdefault(key, value)
for key in sorted(values):
    if key in ("include", "match", "usepam"):
        continue
    value = values[key]
    print(key, "without-password" if value == "prohibit-password" else value)
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
case "$1" in
  cat) [[ "$2" == "${FAKE_UNIT:-ssh.service}" ]] ;;
  try-reload-or-restart) exit "${FAKE_RELOAD_FAILS:-0}" ;;
  *) exit 0 ;;
esac
"""

FAKE_SYSTEMD_RUN = """#!/usr/bin/env bash
echo "systemd-run $*" >> "$FAKE_LOG"
exit "${FAKE_TIMER_FAILS:-0}"
"""

FAKE_SSH_KEYGEN = """#!/usr/bin/env bash
n=0
while IFS= read -r line; do
  [[ -z "$line" || "$line" == \\#* ]] && continue
  n=$((n + 1))
  echo "256 SHA256:fingerprint$n person@example.org (ED25519)"
done < "$2"
((n > 0))
"""

FAKE_JOURNALCTL = """#!/usr/bin/env bash
echo "journalctl $*" >> "$FAKE_LOG"
if [[ " $* " == *" --since @"* ]]; then
  cat "${FAKE_JOURNAL_NEW:-/dev/null}"
else
  cat "${FAKE_JOURNAL:-/dev/null}"
fi
"""

FAKE_SS = """#!/usr/bin/env bash
cat <<'EOF'
LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 128 [::]:22 [::]:*
LISTEN 0 4096 *:443 *:*
LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*
LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*
EOF
"""

KEY_LOGINS = """\
Accepted publickey for root from 203.0.113.7 port 51234 ssh2: ED25519 SHA256:abc
Accepted publickey for root from 203.0.113.7 port 51240 ssh2: ED25519 SHA256:abc
Accepted password for root from 198.51.100.9 port 40000 ssh2
Invalid user admin from 192.0.2.44 port 1111
Failed password for invalid user admin from 192.0.2.44 port 1111 ssh2
Failed password for root from 192.0.2.45 port 2222 ssh2
"""
PASSWORD_ONLY = "Accepted password for root from 198.51.100.9 port 40000 ssh2\n"
NEW_KEY_LOGIN = "Accepted publickey for root from 203.0.113.8 port 6000 ssh2\n"


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.etc = tmp / "etc"
        self.dropin_dir = self.etc / "ssh" / "sshd_config.d"
        self.dropin = self.dropin_dir / "00-cyberdefency-hardening.conf"
        self.before = Path(f"{self.dropin}.before")
        self.pending = self.etc / "ssh" / ".hardening-pending"
        self.keys = tmp / "authorized_keys"
        self.log = tmp / "calls.log"
        self.journal = tmp / "journal.txt"
        self.journal_new = tmp / "journal-new.txt"
        self.extra: dict[str, str] = {}

        self.dropin_dir.mkdir(parents=True)
        (self.etc / "ssh" / "sshd_config").write_text(
            "Include /etc/ssh/sshd_config.d/*.conf\nUsePAM yes\n", encoding="utf-8"
        )
        self.keys.write_text("ssh-ed25519 AAAAC3Nza phone\n", encoding="utf-8")
        self.journal.write_text(KEY_LOGINS, encoding="utf-8")
        self.journal_new.write_text("", encoding="utf-8")

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        tools = {
            "sshd": FAKE_SSHD,
            "systemctl": FAKE_SYSTEMCTL,
            "systemd-run": FAKE_SYSTEMD_RUN,
            "ssh-keygen": FAKE_SSH_KEYGEN,
            "journalctl": FAKE_JOURNALCTL,
            "ss": FAKE_SS,
        }
        for name, body in tools.items():
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.bin_dir = bin_dir

    def run(self, *args: str, **extra: str) -> tuple[int, str, list[str]]:
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "ETC_DIR": str(self.etc),
            "AUTH_KEYS": str(self.keys),
            "AUTH_LOG": str(self.tmp / "no-auth.log"),
            "FAKE_LOG": str(self.log),
            "FAKE_JOURNAL": str(self.journal),
            "FAKE_JOURNAL_NEW": str(self.journal_new),
            **self.extra,
            **extra,
        }
        result = subprocess.run(  # noqa: S603
            ["bash", str(SCRIPT), *args],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        calls = (
            self.log.read_text(encoding="utf-8").splitlines()
            if self.log.exists()
            else []
        )
        return result.returncode, result.stdout + result.stderr, calls

    def reloads(self, calls: list[str]) -> list[str]:
        return [c for c in calls if c.startswith("systemctl try-reload-or-restart")]

    def timers(self, calls: list[str]) -> list[str]:
        return [c for c in calls if c.startswith("systemd-run")]


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def untouched(box: Box, calls: list[str]) -> None:
    assert not box.dropin.exists()
    assert not box.before.exists()
    assert not box.pending.exists()
    assert not box.timers(calls)
    assert not box.reloads(calls)


# ---------------------------------------------------------------- the report
def test_the_report_changes_nothing_and_shows_what_matters(box: Box) -> None:
    (box.etc / "ssh" / "sshd_config").write_text(
        "Include /etc/ssh/sshd_config.d/*.conf\nMatch Address 10.0.0.0/8\n",
        encoding="utf-8",
    )
    code, out, calls = box.run()
    assert code == 0, out
    assert "REPORT ONLY" in out
    assert "password login" in out
    assert "yes" in out
    assert "(ED25519) SHA256:fingerprint1" in out
    assert "person@example.org" not in out  # a key comment is often an e-mail
    assert "root from 203.0.113.7  x2" in out
    assert "root from 198.51.100.9  x1" in out
    assert "refused guesses: 3 log lines from 2 addresses" in out
    assert "Match" in out
    assert "22 443" in out
    assert "8080" not in out  # loopback is not "open to the internet"
    assert "set: PasswordAuthentication no" in out
    assert "SSH_HARDENING_REPORT_DONE" in out
    untouched(box, calls)


def test_the_report_says_when_there_is_nothing_to_do(box: Box) -> None:
    defaults = "passwordauthentication=no,x11forwarding=no"
    code, out, _ = box.run(FAKE_DEFAULTS=defaults)
    assert code == 0, out
    assert "already as strict" in out


# ------------------------------------------------------------------- --apply
def test_apply_writes_one_file_arms_the_timer_then_reloads(box: Box) -> None:
    code, out, calls = box.run("--apply")
    assert code == 0, out
    assert "SSH_HARDENING_APPLIED" in out
    assert "--confirm" in out
    text = box.dropin.read_text(encoding="utf-8")
    assert "PasswordAuthentication no" in text
    assert "X11Forwarding no" in text
    # root already only by key and PAM off in the stand-in: nothing else is written
    assert "PermitRootLogin" not in text
    assert "KbdInteractive" not in text
    assert re.fullmatch(r"\d+\n", box.pending.read_text(encoding="utf-8"))

    timers = box.timers(calls)
    assert len(timers) == 1
    assert "--unit=ssh-hardening-revert" in timers[0]
    assert "--on-active=600s" in timers[0]
    assert f"{SCRIPT} --revert" in timers[0]
    # the way back is armed BEFORE sshd is told to reload
    first_run = calls.index(timers[0])
    first_reload = calls.index("systemctl try-reload-or-restart ssh")
    assert first_run < first_reload


def test_a_cloud_image_file_that_sorts_later_cannot_win(box: Box) -> None:
    (box.dropin_dir / "50-cloud-init.conf").write_text(
        "PasswordAuthentication yes\n", encoding="utf-8"
    )
    code, out, _ = box.run("--apply")
    assert code == 0, out
    assert "SSH_HARDENING_APPLIED" in out


def test_a_file_that_sorts_earlier_is_found_and_nothing_is_left_behind(
    box: Box,
) -> None:
    (box.dropin_dir / "00-aaa.conf").write_text(
        "PasswordAuthentication yes\n", encoding="utf-8"
    )
    code, out, calls = box.run("--apply")
    assert code == 1
    assert "not in force" in out
    assert "PasswordAuthentication" in out
    untouched(box, calls)


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("permitrootlogin=yes", "PermitRootLogin prohibit-password"),
        ("permitemptypasswords=yes", "PermitEmptyPasswords no"),
        ("kbdinteractiveauthentication=yes", "KbdInteractiveAuthentication no"),
    ],
)
def test_it_tightens_what_is_loose(box: Box, setting: str, expected: str) -> None:
    code, out, _ = box.run("--apply", FAKE_DEFAULTS=setting)
    assert code == 0, out
    assert expected in box.dropin.read_text(encoding="utf-8")


def test_it_never_loosens_a_stricter_setting(box: Box) -> None:
    code, out, _ = box.run("--apply", FAKE_DEFAULTS="permitrootlogin=no")
    assert code == 0, out
    assert "PermitRootLogin" not in box.dropin.read_text(encoding="utf-8")


def test_an_old_openssh_gets_the_old_keyword(box: Box) -> None:
    code, out, _ = box.run(
        "--apply",
        FAKE_OLD_SSHD="1",
        FAKE_DEFAULTS="challengeresponseauthentication=yes",
    )
    assert code == 0, out
    text = box.dropin.read_text(encoding="utf-8")
    assert "ChallengeResponseAuthentication no" in text
    assert "KbdInteractive" not in text


def test_the_service_may_be_called_sshd(box: Box) -> None:
    code, out, calls = box.run("--apply", FAKE_UNIT="sshd.service")
    assert code == 0, out
    assert "systemctl try-reload-or-restart sshd" in calls


def test_already_strict_is_left_alone(box: Box) -> None:
    defaults = "passwordauthentication=no,x11forwarding=no"
    code, out, calls = box.run("--apply", FAKE_DEFAULTS=defaults)
    assert code == 0, out
    assert "SSH_HARDENING_NOTHING_TO_DO" in out
    untouched(box, calls)


# ------------------------------------------------- the refusals: no lock-out
def test_no_key_on_the_server_no_change(box: Box) -> None:
    box.keys.write_text("# only a comment\n", encoding="utf-8")
    code, out, calls = box.run("--apply")
    assert code == 1
    assert "no key in" in out
    untouched(box, calls)


def test_a_key_never_seen_working_is_not_enough(box: Box) -> None:
    box.journal.write_text(PASSWORD_ONLY, encoding="utf-8")
    code, out, calls = box.run("--apply")
    assert code == 1
    assert "no login by key" in out
    untouched(box, calls)


def test_the_auth_log_is_read_when_the_journal_has_nothing(box: Box) -> None:
    box.journal.write_text("", encoding="utf-8")
    auth = box.tmp / "no-auth.log"
    auth.write_text(
        "Nov  8 12:00:00 host sshd[123]: Accepted publickey for root from "
        "203.0.113.7 port 1 ssh2: ED25519 SHA256:x\n",
        encoding="utf-8",
    )
    code, out, _ = box.run("--apply")
    assert code == 0, out


def test_sshd_rejecting_the_file_removes_it(box: Box) -> None:
    code, out, calls = box.run("--apply", FAKE_T_FAILS="1")
    assert code == 1
    assert "rejected" in out
    untouched(box, calls)


def test_an_earlier_version_of_the_file_comes_back_when_the_new_one_fails(
    box: Box,
) -> None:
    box.dropin.write_text("PermitRootLogin prohibit-password\n", encoding="utf-8")
    code, out, _ = box.run("--apply", FAKE_T_FAILS="1")
    assert code == 1, out
    assert box.dropin.read_text(encoding="utf-8") == (
        "PermitRootLogin prohibit-password\n"
    )
    assert not box.before.exists()


def test_a_timer_that_cannot_start_means_no_change(box: Box) -> None:
    code, out, calls = box.run("--apply", FAKE_TIMER_FAILS="1")
    assert code == 1
    assert "safety timer" in out
    assert not box.dropin.exists()
    assert not box.pending.exists()
    assert not box.reloads(calls)


def test_a_reload_that_fails_takes_everything_back(box: Box) -> None:
    code, out, calls = box.run("--apply", FAKE_RELOAD_FAILS="1")
    assert code == 1
    assert "did not reload" in out
    assert not box.dropin.exists()
    assert not box.pending.exists()
    assert "systemctl stop ssh-hardening-revert.timer" in calls


def test_a_second_apply_waits_for_the_first_to_be_settled(box: Box) -> None:
    assert box.run("--apply")[0] == 0
    code, out, _ = box.run("--apply")
    assert code == 1
    assert "already waiting" in out


# ------------------------------------------------- --confirm and --revert
def test_confirm_needs_a_key_login_after_the_change(box: Box) -> None:
    assert box.run("--apply")[0] == 0
    code, out, calls = box.run("--confirm")  # only the old window: no new login
    assert code == 1
    assert "no login by key since the change" in out
    assert box.pending.exists()
    assert box.dropin.exists()
    assert "systemctl stop ssh-hardening-revert.timer" not in calls
    assert any(re.search(r"journalctl .*--since @\d+", c) for c in calls)


def test_confirm_keeps_the_change_and_stops_the_timer(box: Box) -> None:
    box.dropin.write_text("X11Forwarding no\n", encoding="utf-8")
    assert box.run("--apply")[0] == 0
    assert box.before.exists()  # the earlier file waits until the change is kept
    box.journal_new.write_text(NEW_KEY_LOGIN, encoding="utf-8")
    code, out, calls = box.run("--confirm")
    assert code == 0, out
    assert "SSH_HARDENING_OK" in out
    assert box.dropin.exists()
    assert not box.pending.exists()
    assert not box.before.exists()
    assert "systemctl stop ssh-hardening-revert.timer" in calls


def test_confirm_without_a_change_waiting(box: Box) -> None:
    code, out, _ = box.run("--confirm")
    assert code == 1
    assert "nothing to confirm" in out


def test_revert_takes_the_change_back(box: Box) -> None:
    assert box.run("--apply")[0] == 0
    code, out, calls = box.run("--revert")
    assert code == 0, out
    assert "SSH_HARDENING_REVERTED" in out
    assert not box.dropin.exists()
    assert not box.pending.exists()
    assert calls.count("systemctl try-reload-or-restart ssh") == 2  # apply, revert
    assert "systemctl stop ssh-hardening-revert.timer" in calls


def test_an_earlier_run_keeps_its_settings_unless_they_are_set_again(
    box: Box,
) -> None:
    box.dropin.write_text(
        "# an earlier run\npasswordauthentication yes\nX11Forwarding no\n",
        encoding="utf-8",
    )
    code, out, _ = box.run("--apply")
    assert code == 0, out
    lines = box.dropin.read_text(encoding="utf-8").splitlines()
    assert "X11Forwarding no" in lines  # kept
    assert "PasswordAuthentication no" in lines  # set again
    assert "passwordauthentication yes" not in lines  # not left in front of it
    assert sum("assword" in line for line in lines) == 1


def test_revert_restores_the_file_it_replaced(box: Box) -> None:
    box.dropin.write_text("X11Forwarding no\n", encoding="utf-8")
    assert box.run("--apply")[0] == 0
    assert box.run("--revert")[0] == 0
    assert box.dropin.read_text(encoding="utf-8") == "X11Forwarding no\n"


def test_revert_with_nothing_to_undo_is_harmless(box: Box) -> None:
    code, out, calls = box.run("--revert")
    assert code == 0, out
    assert "nothing to undo" in out
    assert not box.reloads(calls)


# ----------------------------------------------------- the doctor's one line
def test_status_is_one_line_for_the_doctor(box: Box) -> None:
    code, out, _ = box.run("--status")
    assert (code, out.strip().split()[0]) == (0, "WARN")
    assert "password login is on" in out

    code, out, _ = box.run("--status", FAKE_DEFAULTS="passwordauthentication=no")
    assert out.strip() == "OK ssh: password login is off, keys only"

    # PAM's keyboard-interactive is a second door for passwords
    code, out, _ = box.run(
        "--status",
        FAKE_DEFAULTS="passwordauthentication=no,kbdinteractiveauthentication=yes",
    )
    assert out.startswith("WARN")

    assert box.run("--apply")[0] == 0
    code, out, _ = box.run("--status")
    assert out.startswith("WARN")
    assert "--confirm" in out


def test_the_doctor_runs_the_status_line() -> None:
    doctor = (ROOT / "scripts" / "update_bot.sh").read_text(encoding="utf-8")
    assert 'ssh_hardening.sh" --status' in doctor.split("--doctor", 3)[-1]


# ------------------------------------------------------------------ hygiene
def test_usage_error(box: Box) -> None:
    code, out, _ = box.run("--bogus")
    assert code == 2
    assert "usage:" in out


def test_the_firewall_the_port_and_the_host_keys_are_not_touched() -> None:
    code_lines = [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    for word in ("ufw", "iptables", "nft ", "ssh-keygen -A", "ssh_host", "Port "):
        assert word not in code
