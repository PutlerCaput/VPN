"""The availability probe for a phone on a Russian mobile network (Termux).

curl and the Termux:API commands are stand-ins; the scripts under test are real.
The probe measures only our own endpoints, Telegram and two control hosts.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

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
rule="$(grep -m1 -E "^($hostport|$host)\|" "$T/curl.rules" 2>/dev/null || true)"
[[ -n "$rule" ]] || rule="default|0|200|0.010|0.030|0.050"
IFS='|' read -r _ rc http tcp tls total <<<"$rule"
printf '%s %s %s %s' "$http" "$tcp" "$tls" "$total"
exit "$rc"
"""
NOTIFY = '#!/bin/bash\necho "$*" >> "$T/notify.log"\n'
WIFI = '#!/bin/bash\ncat "$T/wifi.json" 2>/dev/null\n'
TELEPHONY = '#!/bin/bash\ncat "$T/tel.json" 2>/dev/null\n'
SCHEDULER = '#!/bin/bash\necho "$*" >> "$T/scheduler.log"\n'

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

    def alerts(self) -> str:
        path = self.home / "alerts.log"
        return path.read_text() if path.exists() else ""

    def notifications(self) -> str:
        path = self.tmp / "notify.log"
        return path.read_text() if path.exists() else ""

    def curl_log(self) -> str:
        return (self.tmp / "curl.log").read_text()


@pytest.fixture
def phone(tmp_path: Path) -> Phone:
    made = Phone(tmp_path)
    made.configure(
        PROBE_SITE_URL="https://app.test",
        PROBE_ENTRIES="203.0.113.5:443 203.0.113.5:8443",
    )
    return made


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
        "telegram:api.telegram.org",
        "telegram:t.me",
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
    assert by_target(phone.rows()[-7:])["telegram:t.me"]["status"] == "ok"


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
    assert by_target(phone.rows()[-6:])["entry:203.0.113.7:443"]["status"] == "ok"


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


# --------------------------------------------------------------- the installer
@pytest.fixture
def box(tmp_path: Path) -> Phone:
    made = Phone(tmp_path)
    scheduler = made.bin / "termux-job-scheduler"
    scheduler.write_text(SCHEDULER)
    scheduler.chmod(0o755)
    return made


def install(box: Phone, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(INSTALL), *args],
        env={**box.env, **env},
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
    assert "PROBE_INSTALLED: measuring every 30 min" in done.stdout
    assert "everything answers" in done.stdout
    conf = (box.home / "probe.conf").read_text()
    assert "PROBE_SITE_URL=https://app.test" in conf
    assert "PROBE_ENTRIES=203.0.113.5:443 203.0.113.5:8443" in conf
    assert "PROBE_NET=mobile" in conf
    assert "PROBE_OPERATOR=MTS" in conf
    assert stat.S_IMODE((box.home / "probe.conf").stat().st_mode) == 0o600
    assert stat.S_IMODE((box.home / "vpn_probe.sh").stat().st_mode) == 0o700
    assert stat.S_IMODE(box.home.stat().st_mode) == 0o700
    runner = (box.home / "run.sh").read_text()
    assert runner.startswith("#!/bin/bash\n")
    assert f'export PROBE_HOME="{box.home}"' in runner
    assert f'exec "/bin/bash" "{box.home}/vpn_probe.sh" run' in runner
    scheduled = (box.tmp / "scheduler.log").read_text().strip()
    assert scheduled == (
        f"--job-id 4712 --script {box.home}/run.sh --period-ms 1800000 "
        "--network any --battery-not-low false --persisted true"
    )
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


def test_the_installer_tells_when_nothing_can_schedule_it(tmp_path: Path) -> None:
    plain = Phone(tmp_path)
    done = install(plain, "--site", "https://app.test")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PROBE_INSTALLED_NOT_SCHEDULED" in done.stdout
    assert "Termux:API" in done.stdout
    assert (plain.home / "probe.log").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "give the site and/or at least one --entry"),
        (["--site", "http://app.test"], "--site must look like https"),
        (["--site", "https://app.test/x"], "no path"),
        (["--entry", "203.0.113.5"], "--entry must look like"),
        (["--site", "https://app.test", "--period", "5"], "15..1440"),
        (["--site", "https://app.test", "--period", "soon"], "15..1440"),
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
