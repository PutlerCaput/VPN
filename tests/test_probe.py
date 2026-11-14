"""The availability probe for a phone on a Russian mobile network (Termux).

curl and the Termux:API commands are stand-ins; the scripts under test are real.
The probe measures only our own endpoints, Telegram and two control hosts.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe" / "vpn_probe.sh"
INSTALL = ROOT / "scripts" / "probe" / "install_probe.sh"
START = 1_800_000_000

CURL = r"""#!/bin/bash
echo "$*" >> "$T/curl.log"
url="${@: -1}"
hostport="${url#*://}"
hostport="${hostport%%/*}"
host="${hostport%%:*}"
[[ -f "$T/curl.delay" ]] && sleep "$(cat "$T/curl.delay")"
rule="$(grep -m1 -E "^($hostport|$host)\|" "$T/curl.rules" 2>/dev/null || true)"
[[ -n "$rule" ]] || rule="default|0|200|0.010|0.030|0.050"
IFS='|' read -r _ rc http tcp tls total <<<"$rule"
printf '%s %s %s %s' "$http" "$tcp" "$tls" "$total"
exit "$rc"
"""
NOTIFY = '#!/bin/bash\necho "$*" >> "$T/notify.log"\n'
WIFI = '#!/bin/bash\ncat "$T/wifi.json" 2>/dev/null\n'
TELEPHONY = '#!/bin/bash\ncat "$T/tel.json" 2>/dev/null\n'
# The Android job scheduler, in the four ways it really behaves: it schedules and lists
# the job (works); it only prints that the Termux:API app is not there (play); it exits
# with 0 and does nothing (silent); it never answers (hang).
SCHEDULER = r"""#!/bin/bash
echo "$*" >> "$T/scheduler.log"
mode="$(cat "$T/scheduler.mode" 2>/dev/null || echo works)"
case "$mode" in
  hang) exec sleep 30 ;;
  play) echo "Termux:API is not yet available on Google Play"; exit 0 ;;
  silent) exit 0 ;;
esac
case "$1" in
  --pending) cat "$T/pending.txt" 2>/dev/null || true ;;
  --cancel) rm -f "$T/pending.txt" ;;
  *) echo "Pending Job 4712: periodic" >> "$T/pending.txt" ;;
esac
"""
WAKE_LOCK = '#!/bin/bash\necho lock >> "$T/wake.log"\n'
WAKE_UNLOCK = '#!/bin/bash\necho unlock >> "$T/wake.log"\n'
GETPROP = '#!/bin/bash\ncat "$T/getprop.txt" 2>/dev/null\n'
HANGS = "#!/bin/bash\nsleep 30\n"
NOT_THERE = "Termux:API is not yet available on Google Play\n"

WIFI_ON = '{\n  "supplicant_state": "COMPLETED",\n  "ssid": "home"\n}\n'
WIFI_OFF = '{\n  "supplicant_state": "DISCONNECTED"\n}\n'
MTS = '{\n  "network_operator_name": "MTS RUS",\n  "data_state": "connected"\n}\n'


class Phone:
    def __init__(self, tmp: Path, *, termux: bool = True) -> None:
        self.tmp = tmp
        self.home = tmp / "probe-home"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        stubs = {"curl": CURL}
        if termux:
            stubs |= {
                "termux-notification": NOTIFY,
                "termux-wifi-connectioninfo": WIFI,
                "termux-telephony-deviceinfo": TELEPHONY,
            }
        for name, body in stubs.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.bin = bin_dir
        self.rules: dict[str, str] = {}
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "T": str(tmp),
            "PROBE_HOME": str(self.home),
            "PROBE_NOW": str(START),
        }
        (tmp / "wifi.json").write_text(WIFI_OFF)
        (tmp / "tel.json").write_text(MTS)

    def add_stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def live_env(self, **env: str) -> dict[str, str]:
        """The environment of a phone whose clock runs: runs get their own time."""
        merged = {**self.env, **env}
        merged.pop("PROBE_NOW")
        return merged

    def configure(self, **values: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        lines = [f"{key}={value}" for key, value in values.items()]
        (self.home / "probe.conf").write_text("\n".join(lines) + "\n")

    def rule(
        self,
        host: str,
        *,
        rc: int = 0,
        http: str = "200",
        tcp: str = "0.010",
        tls: str = "0.030",
        total: str = "0.050",
    ) -> None:
        self.rules[host] = f"{host}|{rc}|{http}|{tcp}|{tls}|{total}"
        (self.tmp / "curl.rules").write_text("\n".join(self.rules.values()) + "\n")

    def run(
        self, *args: str, now: int | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        merged = {**self.env, **env}
        if now is not None:
            merged["PROBE_NOW"] = str(now)
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(PROBE), *args],
            env=merged,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def rows(self) -> list[dict[str, str]]:
        lines = (self.home / "probe.log").read_text().splitlines()
        names = lines[0].split("\t")
        return [dict(zip(names, line.split("\t"), strict=True)) for line in lines[1:]]

    def latest(self) -> dict[str, dict[str, str]]:
        """The rows of the newest run, by target."""
        rows = self.rows()
        newest = max(int(row["ts"]) for row in rows)
        return by_target([row for row in rows if int(row["ts"]) == newest])

    def runs_logged(self) -> int:
        path = self.home / "probe.log"
        if not path.exists():
            return 0
        return len({line.split("\t")[0] for line in path.read_text().splitlines()[1:]})

    def wait_for_runs(self, count: int, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while self.runs_logged() < count:
            assert time.monotonic() < deadline, f"no {count} runs within {seconds} s"
            time.sleep(0.1)

    def stop_loop(self) -> None:
        subprocess.run(  # noqa: S603
            ["/bin/bash", str(PROBE), "loop-stop"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def alerts(self) -> str:
        path = self.home / "alerts.log"
        return path.read_text() if path.exists() else ""

    def notifications(self) -> str:
        path = self.tmp / "notify.log"
        return path.read_text() if path.exists() else ""

    def curl_log(self) -> str:
        return (self.tmp / "curl.log").read_text()


@pytest.fixture
def phone(tmp_path: Path) -> Iterator[Phone]:
    made = Phone(tmp_path)
    made.configure(
        PROBE_SITE_URL="https://app.test",
        PROBE_ENTRIES="203.0.113.5:443 203.0.113.5:8443",
    )
    yield made
    made.stop_loop()  # a failing test must not leave a measuring loop behind


def by_target(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["target"]: row for row in rows}


def test_a_run_with_everything_answering_logs_each_target_and_alerts_nobody(
    phone: Phone,
) -> None:
    result = phone.run("run")
    assert result.returncode == 0, result.stderr
    assert "everything answers" in result.stdout
    rows = by_target(phone.rows())
    assert set(rows) == {
        "control:ya.ru",
        "control:connectivitycheck.gstatic.com",
        "site",
        "entry:203.0.113.5:443",
        "entry:203.0.113.5:8443",
        # The bot and the Mini App; the site's login widget and its sign-in.
        "telegram:api.telegram.org",
        "telegram:t.me",
        "telegram:telegram.org",
        "telegram:oauth.telegram.org",
    }
    assert {row["status"] for row in rows.values()} == {"ok"}
    assert {row["valid"] for row in rows.values()} == {"1"}
    assert rows["site"]["total_ms"] == "50"
    assert rows["entry:203.0.113.5:443"]["tcp_ms"] == "10"
    assert phone.alerts() == ""
    assert phone.notifications() == ""
    calls = phone.curl_log()
    assert "https://app.test/healthz" in calls
    assert "-k https://203.0.113.5:443/" in calls
    assert "-I https://t.me/" in calls
    assert "-I https://telegram.org/" in calls
    assert "-I https://oauth.telegram.org/" in calls


def test_a_failing_site_alerts_after_three_runs_and_again_when_it_recovers(
    phone: Phone,
) -> None:
    phone.rule("app.test", rc=28, http="000", tcp="0", tls="0", total="10.0")
    for step in range(2):
        out = phone.run("run", now=START + step * 900)
        assert "not answering: site" in out.stdout
        assert phone.alerts() == ""
    phone.run("run", now=START + 1800)
    assert "Не отвечает: site (запусков подряд: 3" in phone.alerts()
    assert "--title VPN probe" in phone.notifications()
    assert "--id vpn-probe-down-" in phone.notifications()
    assert "Не отвечает: site" in phone.notifications()
    # Still down: it is said once, not at every run.
    phone.run("run", now=START + 2700)
    assert phone.alerts().count("Не отвечает: site") == 1
    # Back: said once.
    phone.rule("app.test")
    phone.run("run", now=START + 3600)
    assert "Снова отвечает: site" in phone.alerts()
    phone.run("run", now=START + 4500)
    assert phone.alerts().count("Снова отвечает") == 1


def test_a_phone_without_internet_says_nothing_about_the_service(
    phone: Phone,
) -> None:
    phone.rule("ya.ru", rc=28, http="000", tcp="0", tls="0", total="10.0")
    phone.rule("app.test", rc=28, http="000", tcp="0", tls="0", total="10.0")
    for step in range(4):
        out = phone.run("run", now=START + step * 900)
        assert "no internet on the phone" in out.stdout
    assert phone.alerts() == ""
    assert {row["valid"] for row in phone.rows()} == {"0"}
    # A failure counted only while the controls answer: one run now is the first.
    phone.rule("ya.ru")
    phone.run("run", now=START + 5000)
    assert phone.alerts() == ""
    state = (phone.home / "state").read_text()
    assert "site fail 1 0" in state


@pytest.mark.parametrize(
    ("rc", "reason"),
    [
        (6, "dns"),
        (7, "refused"),
        (28, "timeout"),
        (35, "tls"),
        (56, "reset"),
        (99, "error"),
    ],
)
def test_the_reason_names_what_happened(phone: Phone, rc: int, reason: str) -> None:
    phone.rule("app.test", rc=rc, http="000", tcp="0", tls="0", total="1.0")
    phone.run("run")
    row = by_target(phone.rows())["site"]
    assert (row["status"], row["reason"]) == ("fail", reason)


def test_a_site_that_answers_with_an_error_page_is_not_up(phone: Phone) -> None:
    phone.rule("app.test", http="503")
    phone.run("run")
    row = by_target(phone.rows())["site"]
    assert (row["status"], row["reason"], row["code"]) == ("fail", "http_503", "503")
    # Telegram and the controls only need an answer of any kind.
    phone.rule("t.me", http="301")
    phone.run("run")
    assert phone.latest()["telegram:t.me"]["status"] == "ok"


def test_an_entry_is_up_when_it_accepts_a_connection(phone: Phone) -> None:
    # A TLS handshake without a name may be refused: the connection was accepted.
    phone.rule("203.0.113.5:443", rc=35, http="000", tcp="0.040", tls="0", total="0.2")
    phone.rule("203.0.113.5:8443", rc=28, http="000", tcp="0", tls="0", total="10.0")
    phone.run("run")
    rows = by_target(phone.rows())
    assert rows["entry:203.0.113.5:443"]["status"] == "ok"
    assert rows["entry:203.0.113.5:443"]["reason"] == "tcp_only"
    assert rows["entry:203.0.113.5:8443"]["status"] == "fail"
    assert rows["entry:203.0.113.5:8443"]["reason"] == "timeout"


def test_a_connection_faster_than_a_millisecond_is_still_a_connection(
    phone: Phone,
) -> None:
    # curl prints exactly 0 when nothing connected; a local connect takes microseconds.
    phone.rule(
        "203.0.113.5:443", rc=35, http="000", tcp="0.0004", tls="0", total="0.001"
    )
    phone.rule("203.0.113.5:8443", rc=7, http="000", tcp="0", tls="0", total="0.0")
    phone.run("run")
    rows = by_target(phone.rows())
    assert rows["entry:203.0.113.5:443"]["status"] == "ok"
    assert rows["entry:203.0.113.5:443"]["tcp_ms"] == "1"
    assert rows["entry:203.0.113.5:8443"]["status"] == "fail"
    assert rows["entry:203.0.113.5:8443"]["tcp_ms"] == "0"


def test_an_entry_with_a_server_name_is_measured_all_the_way(phone: Phone) -> None:
    phone.configure(PROBE_ENTRIES="203.0.113.7:443:cover.example")
    phone.rule("cover.example", rc=35, http="000", tcp="0.040", tls="0", total="0.2")
    phone.run("run")
    row = by_target(phone.rows())["entry:203.0.113.7:443"]
    assert row["status"] == "fail"
    assert "--resolve cover.example:443:203.0.113.7" in phone.curl_log()
    assert "https://cover.example:443/" in phone.curl_log()
    phone.rule("cover.example", http="200")
    phone.run("run")
    assert phone.latest()["entry:203.0.113.7:443"]["status"] == "ok"


def test_the_network_is_named_from_the_termux_api(phone: Phone) -> None:
    phone.run("run")
    assert {(r["net"], r["operator"]) for r in phone.rows()} == {("mobile", "MTS_RUS")}
    (phone.tmp / "wifi.json").write_text(WIFI_ON)
    phone.run("run", now=START + 900)
    assert ("wifi", "MTS_RUS") in {(r["net"], r["operator"]) for r in phone.rows()}
    # The settings can name it by hand.
    phone.configure(
        PROBE_SITE_URL="https://app.test",
        PROBE_ENTRIES="203.0.113.5:443",
        PROBE_NET="mobile",
        PROBE_OPERATOR='"Beeline"',
    )
    phone.run("run", now=START + 1800)
    assert phone.rows()[-1]["net"] == "mobile"
    assert phone.rows()[-1]["operator"] == "Beeline"


def test_without_termux_api_the_network_is_unknown(tmp_path: Path) -> None:
    plain = Phone(tmp_path, termux=False)
    plain.configure(PROBE_SITE_URL="https://app.test")
    result = plain.run("run")
    assert result.returncode == 0, result.stderr
    assert {(r["net"], r["operator"]) for r in plain.rows()} == {("unknown", "unknown")}


def test_a_notice_instead_of_an_answer_is_not_a_network(phone: Phone) -> None:
    # termux-api is installed but the Termux:API app is not: the commands print a
    # notice, not JSON. That is "unknown", never "mobile".
    (phone.tmp / "wifi.json").write_text(NOT_THERE)
    (phone.tmp / "tel.json").write_text(NOT_THERE)
    phone.run("run")
    assert {(r["net"], r["operator"]) for r in phone.rows()} == {("unknown", "unknown")}
    # An empty answer is no answer either.
    (phone.tmp / "wifi.json").write_text("")
    phone.run("run", now=START + 900)
    assert {r["net"] for r in phone.rows()} == {"unknown"}


def test_the_operator_comes_from_the_system_when_the_app_has_none(
    tmp_path: Path,
) -> None:
    plain = Phone(tmp_path, termux=False)
    plain.configure(PROBE_SITE_URL="https://app.test")
    plain.add_stub("getprop", GETPROP)
    (tmp_path / "getprop.txt").write_text("MTS RUS,\n")
    plain.run("run")
    assert {(r["net"], r["operator"]) for r in plain.rows()} == {("unknown", "MTS_RUS")}
    # No such property or an empty one: unknown, not an empty name.
    (tmp_path / "getprop.txt").write_text("\n")
    plain.run("run", now=START + 900)
    assert plain.latest()["site"]["operator"] == "unknown"
    # Two slots: "slot1,slot2". The name of the one that has a SIM is taken, whichever
    # slot it is in.
    for answer, expected in [
        ("Sovcombank,\n", "Sovcombank"),
        (",MTS RUS\n", "MTS_RUS"),
        ("MTS RUS,Beeline\n", "MTS_RUS"),
        (",\n", "unknown"),
    ]:
        (tmp_path / "getprop.txt").write_text(answer)
        plain.run("run", now=START + 1800)
        assert plain.latest()["site"]["operator"] == expected, answer


def test_the_manual_network_name_beats_every_detection(tmp_path: Path) -> None:
    plain = Phone(tmp_path, termux=False)
    plain.configure(
        PROBE_SITE_URL="https://app.test", PROBE_NET="mobile", PROBE_OPERATOR="Tele2"
    )
    plain.add_stub("getprop", GETPROP)
    (tmp_path / "getprop.txt").write_text("MTS RUS,\n")
    plain.run("run")
    assert {(r["net"], r["operator"]) for r in plain.rows()} == {("mobile", "Tele2")}


def test_termux_api_commands_that_never_answer_do_not_hang_a_run(
    phone: Phone,
) -> None:
    # Without the Termux:API app some termux-* commands wait for ever.
    for name in ("termux-wifi-connectioninfo", "termux-telephony-deviceinfo"):
        phone.add_stub(name, HANGS)
    phone.add_stub("termux-notification", HANGS)
    phone.configure(PROBE_SITE_URL="https://app.test", PROBE_FAIL_STREAK="1")
    phone.rule("app.test", rc=28, http="000", tcp="0", tls="0", total="10.0")
    began = time.monotonic()
    out = phone.run("run", PROBE_API_TIMEOUT="1")
    assert time.monotonic() - began < 25
    assert out.returncode == 0, out.stderr
    assert {(r["net"], r["operator"]) for r in phone.rows()} == {("unknown", "unknown")}
    # The alert was written even though showing it never worked.
    assert "Не отвечает: site" in phone.alerts()


def test_telegram_going_dark_is_one_message_not_four(phone: Phone) -> None:
    telegram = ("api.telegram.org", "t.me", "telegram.org", "oauth.telegram.org")
    for host in telegram:
        phone.rule(host, rc=28, http="000", tcp="0", tls="0", total="10.0")
    for step in range(3):
        phone.run("run", now=START + step * 900)
    assert phone.alerts().count("Не отвечает") == 1
    assert len(phone.notifications().splitlines()) == 1
    down = phone.alerts().strip().split("\t")[1]
    assert down == (
        "Не отвечает: telegram:api.telegram.org, telegram:t.me, telegram:telegram.org, "
        "telegram:oauth.telegram.org (запусков подряд: 3, сеть mobile/MTS_RUS)"
    )
    # Two of them come back: said once, with their names; the others stay quiet.
    phone.rule("api.telegram.org")
    phone.rule("t.me")
    phone.run("run", now=START + 3 * 900)
    assert phone.alerts().count("Снова отвечает") == 1
    assert "Снова отвечает: telegram:api.telegram.org, telegram:t.me (сеть" in (
        phone.alerts()
    )
    phone.run("run", now=START + 4 * 900)
    assert phone.alerts().count("Снова отвечает") == 1
    # The rest return later: their own message.
    phone.rule("telegram.org")
    phone.rule("oauth.telegram.org")
    phone.run("run", now=START + 5 * 900)
    assert phone.alerts().count("Снова отвечает") == 2
    assert "Снова отвечает: telegram:telegram.org, telegram:oauth.telegram.org" in (
        phone.alerts()
    )
    assert phone.alerts().count("Не отвечает") == 1


def test_a_site_and_a_telegram_target_failing_together_are_one_message(
    phone: Phone,
) -> None:
    phone.rule("app.test", rc=28, http="000", tcp="0", tls="0", total="10.0")
    phone.rule("t.me", rc=28, http="000", tcp="0", tls="0", total="10.0")
    for step in range(3):
        phone.run("run", now=START + step * 900)
    assert phone.alerts().count("Не отвечает") == 1
    assert "Не отвечает: site, telegram:t.me (запусков подряд: 3" in phone.alerts()


def test_the_summary_groups_by_network_and_leaves_out_runs_without_internet(
    phone: Phone,
) -> None:
    phone.run("run", now=START)
    phone.rule("app.test", rc=28, http="000", tcp="0", tls="0", total="10.0")
    phone.run("run", now=START + 900)
    (phone.tmp / "wifi.json").write_text(WIFI_ON)
    phone.rule("app.test")
    phone.run("run", now=START + 1800)
    phone.rule("ya.ru", rc=28, http="000", tcp="0", tls="0", total="10.0")
    phone.run("run", now=START + 2700)
    out = phone.run("summary", "24", now=START + 3600)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    mobile_site = next(
        line for line in lines if line.startswith("mobile/MTS_RUS") and " site " in line
    )
    assert "50%" in mobile_site
    wifi_site = next(
        line for line in lines if line.startswith("wifi/") and " site " in line
    )
    assert "100%" in wifi_site
    assert "last failure" in lines[1]
    assert "timeout" in mobile_site
    assert "left out: 1 runs with no internet" in out.stdout
    # An hour window sees only the last runs.
    recent = phone.run("summary", "1", now=START + 3600 + 3600)
    assert "no runs in this period" in recent.stdout
    assert phone.run("summary", "soon").returncode == 1


def test_last_and_status_say_how_it_stands(phone: Phone) -> None:
    assert phone.run("status").stdout.startswith("WARN probe: no measurements yet")
    phone.run("run", now=START)
    status = phone.run("status", now=START + 120)
    assert (
        status.stdout.strip() == "OK probe: last run 2 min ago, nothing of ours failing"
    )
    last = phone.run("last")
    assert "site" in last.stdout
    assert "controls answered" in last.stdout
    old = phone.run("status", now=START + 100 * 60)
    assert "WARN probe: the last run was 100 min ago" in old.stdout
    phone.rule("app.test", rc=7, http="000", tcp="0", tls="0", total="0.1")
    phone.run("run", now=START + 200 * 60)
    failing = phone.run("status", now=START + 200 * 60)
    assert "WARN probe: not answering: site" in failing.stdout


def test_the_settings_file_is_read_never_run(phone: Phone, tmp_path: Path) -> None:
    pwned = tmp_path / "pwned"
    phone.configure(
        PROBE_SITE_URL="https://app.test",
        PROBE_NET='"mobile"',
        EXTRA=f"$(touch {pwned})",
    )
    with (phone.home / "probe.conf").open("a") as conf:
        conf.write(f"PROBE_OPERATOR=$(touch {pwned}-2)\n")
        conf.write(f"`touch {pwned}-3`\n")
    phone.run("run")
    for suffix in ("", "-2", "-3"):
        assert not Path(f"{pwned}{suffix}").exists()
    assert phone.rows()[0]["net"] == "mobile"  # quotes stripped
    # The value that looks like a command is a name, made harmless in the log.
    assert "(" not in phone.rows()[0]["operator"]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"PROBE_SITE_URL": "http://app.test"}, "PROBE_SITE_URL must look like https"),
        ({"PROBE_SITE_URL": "https://app.test/x"}, "no path"),
        ({"PROBE_SITE_URL": "https://a b"}, "PROBE_SITE_URL must look like"),
        (
            {"PROBE_SITE_URL": "https://app.test", "PROBE_ENTRIES": "203.0.113.5"},
            "an entry must look like",
        ),
        (
            {
                "PROBE_SITE_URL": "https://app.test",
                "PROBE_ENTRIES": "host.test:443:x.y",
            },
            "needs the server's IP address",
        ),
        (
            {"PROBE_SITE_URL": "https://app.test", "PROBE_TIMEOUT": "99"},
            "PROBE_TIMEOUT must be",
        ),
        (
            {"PROBE_SITE_URL": "https://app.test", "PROBE_FAIL_STREAK": "0"},
            "PROBE_FAIL_STREAK must be",
        ),
        (
            {"PROBE_SITE_URL": "https://app.test", "PROBE_CONTROLS": "ftp://x.test"},
            "not a plain URL",
        ),
        (
            {"PROBE_SITE_URL": "https://app.test", "PROBE_PERIOD": "0"},
            "PROBE_PERIOD must be",
        ),
        (
            {"PROBE_SITE_URL": "https://app.test", "PROBE_PERIOD": "5000"},
            "PROBE_PERIOD must be",
        ),
        ({}, "nothing of ours to measure"),
    ],
)
def test_bad_settings_are_refused_before_anything_is_measured(
    phone: Phone, values: dict[str, str], message: str
) -> None:
    phone.configure(**values)
    result = phone.run("run")
    assert result.returncode == 1
    assert message in result.stderr
    assert not (phone.home / "probe.log").exists()
    assert not (phone.tmp / "curl.log").exists()


def test_the_log_is_trimmed_and_keeps_its_header(phone: Phone) -> None:
    for step in range(6):
        phone.run("run", now=START + step * 900, PROBE_MAX_LOG_LINES="20")
    lines = (phone.home / "probe.log").read_text().splitlines()
    assert lines[0].startswith("ts\tnet\toperator\ttarget")
    assert len(lines) <= 21
    assert len(lines) > 7


def test_unknown_commands_are_refused(phone: Phone) -> None:
    result = phone.run("nonsense")
    assert result.returncode == 1
    assert "usage" in result.stderr
    shown = phone.run("config")
    assert "PROBE_SITE_URL=https://app.test" in shown.stdout
    assert "PROBE_TIMEOUT=10" in shown.stdout
    assert "PROBE_PERIOD=15" in shown.stdout
    assert "https://telegram.org/" in shown.stdout
    assert "https://oauth.telegram.org/" in shown.stdout


def test_the_stale_check_follows_the_period(phone: Phone) -> None:
    phone.configure(PROBE_SITE_URL="https://app.test", PROBE_PERIOD="60")
    phone.run("run", now=START)
    assert phone.run("status", now=START + 100 * 60).stdout.startswith("OK probe")
    stale = phone.run("status", now=START + 200 * 60)
    assert "WARN probe: the last run was 200 min ago" in stale.stdout
    assert "schedule (or the loop)" in stale.stdout


# --------------------------------------------------------------------- the loop
def start_loop(phone: Phone, *args: str, **env: str) -> subprocess.Popen[str]:
    """A loop whose "minute" is a second, so that a test can watch it measure."""
    return subprocess.Popen(  # noqa: S603
        ["/bin/bash", str(PROBE), "loop", *args],
        env=phone.live_env(PROBE_LOOP_UNIT="1", **env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_the_loop_measures_again_and_again_until_it_is_stopped(phone: Phone) -> None:
    phone.add_stub("termux-wake-lock", WAKE_LOCK)
    phone.add_stub("termux-wake-unlock", WAKE_UNLOCK)
    loop = start_loop(phone, "1")
    try:
        phone.wait_for_runs(2)
        assert int((phone.home / "loop.pid").read_text()) == loop.pid
        # One loop at a time: a second one is refused and changes nothing.
        second = phone.run("loop", "1")
        assert second.returncode == 1
        assert "a loop is already running" in second.stderr
        stopped = phone.run("loop-stop")
        assert "PROBE_LOOP_STOP" in stopped.stdout
        out, err = loop.communicate(timeout=30)
    finally:
        if loop.poll() is None:
            loop.kill()
    assert loop.returncode == 0, err
    assert "PROBE_LOOP: measuring every 1 min" in out
    assert not (phone.home / "loop.pid").exists()
    # The phone stays awake while measuring and is let go afterwards, once.
    assert (phone.tmp / "wake.log").read_text().split() == ["lock", "unlock"]
    assert "everything answers" in (phone.home / "run.out").read_text()
    # Stopped means stopped: no further runs appear.
    runs = phone.runs_logged()
    time.sleep(2.5)
    assert phone.runs_logged() == runs


def test_a_wake_lock_that_never_answers_does_not_block_the_loop(phone: Phone) -> None:
    phone.add_stub("termux-wake-lock", HANGS)
    phone.add_stub("termux-wake-unlock", HANGS)
    loop = start_loop(phone, "1", PROBE_API_TIMEOUT="1")
    try:
        phone.wait_for_runs(1, seconds=20)
        began = time.monotonic()
        phone.run("loop-stop")
        out, err = loop.communicate(timeout=30)
        # The unlock hangs as well: it is given up after the timeout, not waited for.
        assert time.monotonic() - began < 15
    finally:
        if loop.poll() is None:
            loop.kill()
    assert loop.returncode == 0, err
    assert "PROBE_LOOP: measuring every 1 min" in out


def test_the_loop_keeps_its_output_file_small(phone: Phone) -> None:
    phone.home.mkdir(parents=True, exist_ok=True)
    (phone.home / "run.out").write_text("x" * 250_000)
    loop = start_loop(phone, "1")
    try:
        # The log row is written before the run prints its line to run.out; a stop
        # in between ends that run before it prints. Wait for the line itself.
        deadline = time.monotonic() + 30
        while "PROBE_RUN" not in (phone.home / "run.out").read_text():
            assert time.monotonic() < deadline, "no run printed within 30 s"
            time.sleep(0.1)
        phone.run("loop-stop")
        loop.communicate(timeout=30)
    finally:
        if loop.poll() is None:
            loop.kill()
    out = (phone.home / "run.out").read_text()
    assert "xxxx" not in out
    assert out.startswith("PROBE_RUN net=")
    assert len(out) < 2_000


def test_the_loop_takes_its_period_from_the_settings(phone: Phone) -> None:
    phone.configure(PROBE_SITE_URL="https://app.test", PROBE_PERIOD="2")
    loop = start_loop(phone)
    try:
        phone.wait_for_runs(1)
        phone.run("loop-stop")
        out, _ = loop.communicate(timeout=30)
    finally:
        if loop.poll() is None:
            loop.kill()
    assert "PROBE_LOOP: measuring every 2 min" in out


@pytest.mark.parametrize("minutes", ["0", "soon", "-5", "10000", "07"])
def test_a_loop_with_a_bad_period_does_not_start(phone: Phone, minutes: str) -> None:
    result = phone.run("loop", minutes)
    assert result.returncode == 1
    assert "usage: vpn_probe.sh loop" in result.stderr
    assert not (phone.home / "loop.pid").exists()
    assert not (phone.home / "probe.log").exists()


def test_loop_stop_with_nothing_running_is_harmless(phone: Phone) -> None:
    stopped = phone.run("loop-stop")
    assert stopped.returncode == 0
    assert "no loop is running" in stopped.stdout
    # A pid file left behind by a loop that is long gone does not count, and does
    # not stop a new loop from starting.
    gone = subprocess.Popen(["/bin/true"])  # noqa: S603
    gone.wait()
    phone.home.mkdir(parents=True, exist_ok=True)
    (phone.home / "loop.pid").write_text(f"{gone.pid}\n")
    assert "no loop is running" in phone.run("loop-stop").stdout
    assert not (phone.home / "loop.pid").exists()
    (phone.home / "loop.pid").write_text(f"{gone.pid}\n")
    loop = start_loop(phone, "1")
    try:
        phone.wait_for_runs(1)
        assert int((phone.home / "loop.pid").read_text()) == loop.pid
    finally:
        phone.stop_loop()
        loop.communicate(timeout=30)
    assert loop.returncode == 0


def test_a_pid_file_never_makes_the_probe_kill_a_stranger(phone: Phone) -> None:
    # After a reboot the number in loop.pid can belong to any process.
    stranger = subprocess.Popen(["/bin/sleep", "30"])  # noqa: S603
    try:
        phone.home.mkdir(parents=True, exist_ok=True)
        (phone.home / "loop.pid").write_text(f"{stranger.pid}\n")
        stopped = phone.run("loop-stop")
        assert "no loop is running" in stopped.stdout
        assert stranger.poll() is None
        assert not (phone.home / "loop.pid").exists()
        # ... and it does not stand in the way of a new loop either.
        (phone.home / "loop.pid").write_text(f"{stranger.pid}\n")
        loop = start_loop(phone, "1")
        try:
            phone.wait_for_runs(1)
            assert int((phone.home / "loop.pid").read_text()) == loop.pid
        finally:
            phone.stop_loop()
            loop.communicate(timeout=30)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_a_loop_stops_at_once_even_in_the_middle_of_a_measurement(
    phone: Phone,
) -> None:
    # Every request takes 3 s: a whole run would take half a minute.
    (phone.tmp / "curl.delay").write_text("3")
    loop = start_loop(phone, "1")
    try:
        deadline = time.monotonic() + 20
        while not (phone.tmp / "curl.log").exists():
            assert time.monotonic() < deadline
            time.sleep(0.1)
        began = time.monotonic()
        stopped = phone.run("loop-stop")
        out, err = loop.communicate(timeout=10)
        assert time.monotonic() - began < 8
    finally:
        if loop.poll() is None:
            loop.kill()
    assert "PROBE_LOOP_STOPPED" in stopped.stdout, stopped.stdout
    assert loop.returncode == 0, err
    assert not (phone.home / "loop.pid").exists()
    # The unfinished measurement left no half-written run behind.
    assert not (phone.home / "probe.log").exists()


# --------------------------------------------------------------- the installer
def wait_until_gone(pid: int, seconds: float = 10) -> bool:
    """True once the process has ended (a zombie nobody collected is ended too)."""
    deadline = time.monotonic() + seconds
    while True:
        state = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Phone]:
    made = Phone(tmp_path)
    made.add_stub("termux-job-scheduler", SCHEDULER)
    yield made
    made.stop_loop()


def install(
    box: Phone, *args: str, live: bool = False, **env: str
) -> subprocess.CompletedProcess[str]:
    base = box.live_env() if live else box.env
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(INSTALL), *args],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_installer_installs_for_real(box: Phone) -> None:
    done = install(
        box,
        "--site",
        "https://app.test",
        "--entry",
        "203.0.113.5:443",
        "--entry",
        "203.0.113.5:8443",
        "--period",
        "30",
        "--net",
        "mobile",
        "--operator",
        "MTS",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PROBE_INSTALLED: measuring every 30 min (job 4712 is in the schedule)" in (
        done.stdout
    )
    assert "everything answers" in done.stdout
    # A job in the list is not yet a run: the installer says how to see one.
    assert "in 35 minutes" in done.stdout
    assert "vpn_probe.sh status" in done.stdout
    # What the first run saw is shown, target by target.
    assert "== What it saw" in done.stdout
    assert "target" in done.stdout
    assert "entry:203.0.113.5:443" in done.stdout
    assert "telegram:oauth.telegram.org" in done.stdout
    conf = (box.home / "probe.conf").read_text()
    assert "PROBE_SITE_URL=https://app.test" in conf
    assert "PROBE_ENTRIES=203.0.113.5:443 203.0.113.5:8443" in conf
    assert "PROBE_PERIOD=30" in conf
    assert "PROBE_NET=mobile" in conf
    assert "PROBE_OPERATOR=MTS" in conf
    assert stat.S_IMODE((box.home / "probe.conf").stat().st_mode) == 0o600
    assert stat.S_IMODE((box.home / "vpn_probe.sh").stat().st_mode) == 0o700
    assert stat.S_IMODE(box.home.stat().st_mode) == 0o700
    runner = (box.home / "run.sh").read_text()
    assert runner.startswith("#!/bin/bash\n")
    assert f'export PROBE_HOME="{box.home}"' in runner
    assert f'exec "/bin/bash" "{box.home}/vpn_probe.sh" run' in runner
    asked, listed = (box.tmp / "scheduler.log").read_text().splitlines()
    assert asked == (
        f"--job-id 4712 --script {box.home}/run.sh --period-ms 1800000 "
        "--network any --battery-not-low false --persisted true"
    )
    # The schedule is looked up afterwards, not trusted.
    assert listed == "--pending"
    # What the scheduler starts really measures.
    before = len((box.home / "probe.log").read_text().splitlines())
    started = subprocess.run(  # noqa: S603
        [str(box.home / "run.sh")],
        env=box.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    after = len((box.home / "probe.log").read_text().splitlines())
    assert after > before
    assert "everything answers" in (box.home / "run.out").read_text()


@pytest.mark.parametrize(
    ("mode", "outcome", "words"),
    [
        # The Google Play build of Termux has no Termux:API app: the command only
        # says so, and exits with 0.
        ("play", "PROBE_INSTALLED_NOT_SCHEDULED", ["Google Play", "--loop"]),
        # Exit 0, no word, and the job is not in the list.
        ("silent", "PROBE_INSTALLED_UNVERIFIED", ["--pending", "--loop"]),
        # Never answers: the installer does not wait for it for ever.
        ("hang", "PROBE_INSTALLED_UNVERIFIED", ["--loop"]),
    ],
)
def test_the_installer_does_not_claim_a_schedule_that_is_not_there(
    box: Phone, mode: str, outcome: str, words: list[str]
) -> None:
    (box.tmp / "scheduler.mode").write_text(mode)
    began = time.monotonic()
    done = install(box, "--site", "https://app.test", PROBE_API_TIMEOUT="1")
    assert time.monotonic() - began < 40
    assert done.returncode == 0, done.stdout + done.stderr
    assert outcome in done.stdout
    assert "PROBE_INSTALLED:" not in done.stdout
    for word in words:
        assert word in done.stdout
    # The first measurement is made and shown either way.
    assert "== What it saw" in done.stdout
    assert (box.home / "probe.log").exists()


def test_the_installer_tells_when_there_is_no_scheduler_at_all(tmp_path: Path) -> None:
    plain = Phone(tmp_path)
    done = install(plain, "--site", "https://app.test")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PROBE_INSTALLED_NOT_SCHEDULED" in done.stdout
    assert "Termux:API" in done.stdout
    assert "--loop" in done.stdout
    assert (plain.home / "probe.log").exists()


def test_the_installer_starts_a_loop_when_asked_and_uninstall_stops_it(
    box: Phone,
) -> None:
    done = install(
        box,
        "--site",
        "https://app.test",
        "--period",
        "1",
        "--net",
        "mobile",
        "--loop",
        live=True,
        PROBE_LOOP_UNIT="1",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PROBE_INSTALLED: measuring every 1 min in a loop" in done.stdout
    assert "nohup bash" in done.stdout
    assert "PROBE_INSTALLED_NOT" not in done.stdout
    # The job scheduler is only asked what it holds: nothing is scheduled, nothing
    # needs cancelling.
    asked = (box.tmp / "scheduler.log").read_text().split("\n")
    assert {line for line in asked if line} == {"--pending"}
    first = int((box.home / "loop.pid").read_text())
    box.wait_for_runs(2)
    assert "PROBE_PERIOD=1" in (box.home / "probe.conf").read_text()
    # Installing again replaces the loop: one measures, the old one is gone.
    again = install(
        box,
        "--site",
        "https://app.test",
        "--period",
        "1",
        "--loop",
        live=True,
        PROBE_LOOP_UNIT="1",
    )
    assert again.returncode == 0, again.stdout + again.stderr
    second = int((box.home / "loop.pid").read_text())
    assert second != first
    assert wait_until_gone(first)
    assert not wait_until_gone(second, seconds=0.5)
    removed = install(box, "--uninstall")
    assert removed.returncode == 0
    assert "PROBE_UNINSTALLED" in removed.stdout
    assert not (box.home / "loop.pid").exists()
    runs = box.runs_logged()
    time.sleep(2.5)
    assert box.runs_logged() == runs
    assert (box.home / "probe.log").exists()
    assert (box.home / "probe.conf").exists()


def test_a_loop_is_refused_while_the_android_job_runs_the_probe(box: Phone) -> None:
    install(box, "--site", "https://app.test")
    assert "4712" in (box.tmp / "pending.txt").read_text()
    # Two runners would measure twice and count every failure twice.
    refused = box.run("loop", "1")
    assert refused.returncode == 1
    assert "already runs the probe (job 4712)" in refused.stderr
    assert "--cancel --job-id 4712" in refused.stderr
    assert not (box.home / "loop.pid").exists()


def test_installing_the_loop_cancels_the_android_job_and_the_job_stops_a_loop(
    box: Phone,
) -> None:
    install(box, "--site", "https://app.test")
    assert "4712" in (box.tmp / "pending.txt").read_text()
    # The loop replaces the job ...
    looped = install(
        box,
        "--site",
        "https://app.test",
        "--period",
        "1",
        "--loop",
        live=True,
        PROBE_LOOP_UNIT="1",
    )
    assert looped.returncode == 0, looped.stdout + looped.stderr
    assert "The Android job 4712 was cancelled" in looped.stdout
    assert "--cancel --job-id 4712" in (box.tmp / "scheduler.log").read_text()
    assert not (box.tmp / "pending.txt").exists()
    loop_pid = int((box.home / "loop.pid").read_text())
    box.wait_for_runs(2)
    # ... and the job replaces the loop.
    scheduled = install(box, "--site", "https://app.test")
    assert scheduled.returncode == 0, scheduled.stdout + scheduled.stderr
    assert "PROBE_INSTALLED: measuring every 15 min (job 4712" in scheduled.stdout
    assert wait_until_gone(loop_pid)
    assert not (box.home / "loop.pid").exists()
    assert "4712" in (box.tmp / "pending.txt").read_text()


def test_a_short_period_is_only_for_the_loop(box: Phone) -> None:
    refused = install(box, "--site", "https://app.test", "--period", "5")
    assert refused.returncode == 1
    assert "--period under 15 minutes needs --loop" in refused.stderr
    assert not box.home.exists()
    accepted = install(
        box, "--site", "https://app.test", "--period", "5", "--loop", live=True
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "measuring every 5 min in a loop" in accepted.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "give the site and/or at least one --entry"),
        (["--site", "http://app.test"], "--site must look like https"),
        (["--site", "https://app.test/x"], "no path"),
        (["--entry", "203.0.113.5"], "--entry must look like"),
        (["--site", "https://app.test", "--period", "soon"], "1..1440"),
        (["--site", "https://app.test", "--period", "0", "--loop"], "1..1440"),
        (["--site", "https://app.test", "--period", "09", "--loop"], "1..1440"),
        (["--site", "https://app.test", "--period", "2000", "--loop"], "1..1440"),
        (["--site", "https://app.test", "--net", "5g"], "--net is mobile or wifi"),
        (["--site", "https://app.test", "--operator", "a b"], "--operator is one word"),
        (["--site"], "--site needs a value"),
        (["--nope"], "unknown option"),
    ],
)
def test_the_installer_refuses_bad_arguments_and_changes_nothing(
    box: Phone, args: list[str], message: str
) -> None:
    result = install(box, *args)
    assert result.returncode == 1
    assert message in result.stderr
    assert not box.home.exists()
    assert not (box.tmp / "scheduler.log").exists()


def test_uninstall_stops_the_schedule_and_keeps_the_log(box: Phone) -> None:
    install(box, "--site", "https://app.test")
    removed = install(box, "--uninstall")
    assert removed.returncode == 0
    assert "PROBE_UNINSTALLED" in removed.stdout
    assert "--cancel --job-id 4712" in (box.tmp / "scheduler.log").read_text()
    assert not (box.home / "run.sh").exists()
    assert (box.home / "probe.log").exists()
    assert (box.home / "probe.conf").exists()
