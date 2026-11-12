"""The monthly auto-update (server_auto_update.sh), its installer, the doctor's
line and the source digest that keeps it from deploying code.

The scripts run for real, with stand-ins for docker, apt, df, free, pgrep and
systemctl, and stubs for the helper scripts (the doctor, the two updaters, the
backup). What is checked is what makes it safe to run when nobody is watching:
it never touches Docker, never removes a package, never rebuilds unless a base
image changed and the code is the code that runs, does nothing while the server
is busy or unwell, and goes back when the doctor finds something new."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DAY = 86400
BASES = ["python:3.11-slim-bookworm", "node:22-bookworm-slim", "caddy:2.11.7"]

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("docker " + " ".join(args) + "\n")
if state.get("down"):
    sys.exit(1)
fmt = args[args.index("--format") + 1] if "--format" in args else ""
fmt = args[args.index("-f") + 1] if "-f" in args else fmt
containers = state["containers"]
by_name = {c["name"]: c for c in containers}


def save():
    json.dump(state, open(state_path, "w"))


if args[:1] == ["ps"]:
    if any(a.startswith("label=") for a in args):
        for c in containers:
            if c.get("web"):
                print(c["name"])
    else:
        for c in containers:
            if "-a" in args or c["running"]:
                print(f"{c['name']}|{c['status']}" if "Status" in fmt else c["name"])
elif args[:1] == ["inspect"]:
    c = by_name.get(args[-1])
    if c is None:
        sys.exit(1)
    if ".State.Running" in fmt:
        print("true" if c["running"] else "false")
    elif "Labels" in fmt:
        print(c.get("source", ""))
    elif ".State.Status" in fmt:
        print("running " + c.get("health", "") if c["running"] else "exited ")
elif args[:2] == ["image", "inspect"]:
    print(state.setdefault("image_ids", {}).get(args[-1], "sha256:old-" + args[-1]))
elif args[:1] == ["pull"]:
    if state.get("pull_fails"):
        sys.exit(1)
    if args[-1] in state.get("pull_changes", []):
        state.setdefault("image_ids", {})[args[-1]] = "sha256:new-" + args[-1]
        save()
elif args[:1] == ["start"]:
    c = by_name.get(args[-1])
    if c is not None and not c.get("stuck"):
        c["running"] = True
        save()
elif args[:1] == ["exec"]:
    if args[3] == "scripts/send_backup.py":
        with open(os.environ["FAKE_SENT"], "a") as sent:
            sent.write(args[5] + "\n")
        print("SENT")
"""

FAKE_APT_GET = r"""#!/usr/bin/env bash
echo "apt-get $*" >> "$FAKE_LOG"
if [[ "$1" == update ]]; then
  [[ "${FAKE_APT_UPDATE_FAIL:-0}" == 1 ]] && exit 100
  exit 0
fi
if [[ "$1" == -s ]]; then
  [[ -n "${FAKE_APT_REMOVES:-}" ]] && echo "Remv ${FAKE_APT_REMOVES} [1.0]"
  [[ "${FAKE_APT_SIM_FAIL:-0}" == 1 ]] && exit 100
  exit 0
fi
[[ "${FAKE_APT_FAIL:-0}" == 1 ]] && exit 100
[[ "${FAKE_APT_NEEDS_REBOOT:-0}" == 1 ]] && touch "$REBOOT_FILE"
[[ "${FAKE_APT_BREAKS_SITE:-0}" == 1 ]] && echo bad > "$FAKE_DOCTOR_FILE"
if [[ -n "${FAKE_APT_STOPS:-}" ]]; then
  python3 - "$FAKE_DOCKER_STATE" "$FAKE_APT_STOPS" <<'PY'
import json, sys
path, name = sys.argv[1:3]
state = json.load(open(path))
for c in state["containers"]:
    if c["name"] == name:
        c["running"] = False
json.dump(state, open(path, "w"))
PY
fi
exit 0
"""

FAKE_APT = r"""#!/usr/bin/env bash
echo "apt $*" >> "$FAKE_LOG"
echo "Listing..."
for pkg in ${FAKE_UPGRADABLE:-}; do
  echo "$pkg/noble-updates 2.0 amd64 [upgradable from: 1.0]"
done
"""

FAKE_DPKG = r"""#!/usr/bin/env bash
echo "dpkg $*" >> "$FAKE_LOG"
[[ "$1" == --audit && -n "${FAKE_DPKG_AUDIT:-}" ]] && echo "${FAKE_DPKG_AUDIT}"
exit 0
"""

FAKE_DPKG_QUERY = r"""#!/usr/bin/env bash
fmt="${2#-f=}"
shift 2
for pkg in "$@"; do
  if [[ "$fmt" == *Package* ]]; then printf '%s=1.0\n' "$pkg"; else printf '1.0'; fi
done
"""

FAKE_APT_CACHE = '#!/usr/bin/env bash\necho "  Candidate: 28.0.1"\n'

FAKE_DF = r"""#!/usr/bin/env bash
echo "Filesystem 1048576-blocks Used Available Capacity Mounted on"
echo "/dev/vda1 30720 8000 ${FAKE_FREE_MB:-20000} 50% /"
"""

FAKE_FREE = r"""#!/usr/bin/env bash
echo "total used free shared buff/cache available"
echo "Mem: 3900 900 1000 50 2000 ${FAKE_AVAIL_MB:-3000}"
echo "Swap: 2047 0 2047"
"""

FAKE_PGREP = r"""#!/usr/bin/env bash
echo "pgrep $*" >> "$FAKE_LOG"
case "${FAKE_BUSY:-}" in
  update) [[ "$*" == *-f* ]] && { echo 4242; exit 0; } ;;
  apt) [[ "$*" == *-x* ]] && { echo 4243; exit 0; } ;;
esac
exit 1
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
[[ "$1" == is-active && "${FAKE_TIMER_INACTIVE:-0}" == 1 ]] && exit 3
exit 0
"""

BOT_STUB = r"""#!/usr/bin/env bash
echo "bot-updater $*" >> "$FAKE_LOG"
case "$1" in
  --doctor)
    if [[ "$(cat "$FAKE_DOCTOR_FILE" 2>/dev/null)" == bad ]]; then
      echo "FAIL site https://example.org/healthz: 502. Bot links lead here"
      echo "DOCTOR: 20 ok, 0 warnings, 1 problems"
      exit 1
    fi
    echo "WARN no weekly cleanup"
    echo "DOCTOR: 21 ok, 1 warnings, 0 problems"
    exit 0 ;;
  --rollback)
    [[ "${FAKE_ROLLBACK_USELESS:-0}" == 1 ]] || echo good > "$FAKE_DOCTOR_FILE"
    echo "ROLLBACK_DONE node-rental-bot:latest <- $2"
    exit 0 ;;
esac
if [[ "${FAKE_BOT_FAIL:-0}" == 1 ]]; then
  echo "UPDATE_FAILED: rolled back to the previous image (healthy)" >&2
  exit 1
fi
[[ "${FAKE_BREAK_AFTER_BOT:-0}" == 1 ]] && echo bad > "$FAKE_DOCTOR_FILE"
echo "ROLLBACK_IMAGE_SAVED node-rental-bot:rollback-T1"
echo "BUILD_OK node-rental-bot:update-T1"
echo "UPDATE_OK node-rental-bot:latest -> node-rental-bot:update-T1"
"""

WEB_STUB = r"""#!/usr/bin/env bash
echo "web-updater $*" >> "$FAKE_LOG"
if [[ "$1" == --rollback ]]; then
  [[ "${FAKE_ROLLBACK_USELESS:-0}" == 1 ]] || echo good > "$FAKE_DOCTOR_FILE"
  echo "WEB_ROLLBACK_DONE $2"
  exit 0
fi
if [[ "${FAKE_WEB_FAIL:-0}" == 1 ]]; then
  echo "WEB_UPDATE_FAILED: restored node-rental-web:rollback-T2" >&2
  exit 1
fi
echo "ROLLBACK_IMAGE node-rental-web:rollback-T2 (was node-rental-web:release-T0)"
echo "WEB_UPDATE_OK node-rental-web:release-T0 -> node-rental-web:release-T3"
"""

BACKUP_STUB = r"""#!/usr/bin/env bash
echo "backup-runner" >> "$FAKE_LOG"
if [[ "${FAKE_BACKUP_FAIL:-0}" == 1 ]]; then
  echo "BACKUP_FAILED: the archive could not be encrypted" >&2
  exit 1
fi
echo "BACKUP_OK /root/server-backups/backup-X.tar.gz.age 10KB telegram=sent"
"""

DIGEST_STUB = '#!/usr/bin/env bash\necho "${FAKE_FOLDER_DIGEST-abc123}"\n'


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "docker": FAKE_DOCKER,
        "apt-get": FAKE_APT_GET,
        "apt": FAKE_APT,
        "dpkg": FAKE_DPKG,
        "dpkg-query": FAKE_DPKG_QUERY,
        "apt-cache": FAKE_APT_CACHE,
        "df": FAKE_DF,
        "free": FAKE_FREE,
        "pgrep": FAKE_PGREP,
        "systemctl": FAKE_SYSTEMCTL,
    }.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in {
        "bot.sh": BOT_STUB,
        "web.sh": WEB_STUB,
        "backup.sh": BACKUP_STUB,
        "digest.sh": DIGEST_STUB,
    }.items():
        (stubs / name).write_text(body, encoding="utf-8")

    up = "Up 2 hours (healthy)"
    containers = [
        {"name": "node-rental-bot-1", "source": "abc123"},
        {"name": "node-rental-web-local-app-web-1", "source": "abc123", "web": True},
        {"name": "marzban-marzban-1"},
    ]
    for container in containers:
        container.update({"running": True, "status": up, "health": "healthy"})
    (tmp_path / "docker.json").write_text(
        json.dumps({"containers": containers, "pull_changes": list(BASES)}),
        encoding="utf-8",
    )
    (tmp_path / "node-backup.env").write_text("AGE_RECIPIENT=x\n", encoding="utf-8")
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("FAKE_")},
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(tmp_path / "docker.json"),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_SENT": str(tmp_path / "sent.txt"),
        "FAKE_DOCTOR_FILE": str(tmp_path / "doctor.txt"),
        "FAKE_UPGRADABLE": "openssl libssl3 docker-ce containerd.io",
        "BOT_UPDATER": str(stubs / "bot.sh"),
        "WEB_UPDATER": str(stubs / "web.sh"),
        "BACKUP_RUNNER": str(stubs / "backup.sh"),
        "DIGEST_TOOL": str(stubs / "digest.sh"),
        "BACKUP_ENV": str(tmp_path / "node-backup.env"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "STATE_DIR": str(tmp_path / "state"),
        "LOCK_FILE": str(tmp_path / "run" / "update.lock"),
        "REBOOT_FILE": str(tmp_path / "reboot-required"),
        "WINDOW_FROM_UTC": "0",
        "WINDOW_TO_UTC": "24",
        "WAIT_CONTAINERS_SECONDS": "2",
        "WAIT_STEP_SECONDS": "1",
    }


def run_script(
    script: str, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPTS / script), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def update(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return run_script("server_auto_update.sh", env, *args)


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def texts(env: dict[str, str]) -> list[str]:
    sent = Path(env["FAKE_SENT"])
    return sent.read_text(encoding="utf-8").splitlines() if sent.exists() else []


def record(env: dict[str, str]) -> dict[str, str]:
    path = Path(env["STATE_DIR"]) / "last-run"
    pairs = (line.split("=", 1) for line in path.read_text("utf-8").splitlines())
    return dict(pairs)


def change_state(env: dict[str, str], **changes: object) -> None:
    path = Path(env["FAKE_DOCKER_STATE"])
    state = json.loads(path.read_text(encoding="utf-8"))
    state.update(changes)
    path.write_text(json.dumps(state), encoding="utf-8")


def change_container(env: dict[str, str], name: str, **changes: object) -> None:
    path = Path(env["FAKE_DOCKER_STATE"])
    state = json.loads(path.read_text(encoding="utf-8"))
    for container in state["containers"]:
        if container["name"] == name:
            container.update(changes)
    path.write_text(json.dumps(state), encoding="utf-8")


def changes(env: dict[str, str]) -> list[str]:
    """Calls that change the server: a package upgrade, a pull, a rebuild, a backup."""
    return [
        c
        for c in calls(env)
        if c.startswith(("apt-get -y", "docker pull", "docker start", "backup-runner"))
        or (c.startswith(("bot-updater", "web-updater")) and "--apply" in c)
        or "--rollback" in c
    ]


def applied(env: dict[str, str]) -> list[str]:
    """Calls that rebuild the bot or the site."""
    return [
        c
        for c in calls(env)
        if c.startswith(("bot-updater", "web-updater")) and "--apply" in c
    ]


def installed(env: dict[str, str]) -> list[str]:
    return [c for c in calls(env) if c.startswith("apt-get -y")]


def doctor_calls(env: dict[str, str]) -> int:
    return len([c for c in calls(env) if c == "bot-updater --doctor"])


# ----------------------------------------------------------------- the update
def test_the_monthly_update_upgrades_packages_but_never_docker(
    env: dict[str, str],
) -> None:
    result = update(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AUTO_UPDATE_OK packages=2 images=rebuilt reboot=no" in result.stdout
    (install,) = installed(env)
    assert install.endswith("install --only-upgrade openssl libssl3")
    assert "docker-ce" not in install
    assert "containerd" not in install
    assert "--force-confold" in install  # a changed config file is kept
    assert "docker-ce 1.0 -> 28.0.1" in result.stdout.replace("\n", " ")
    # Nothing but packages and the two images changes: no pull of the Caddy
    # compiler image, no removal of anything.
    pulled = [c.split()[-1] for c in calls(env) if c.startswith("docker pull")]
    assert sorted(pulled) == sorted(BASES)
    assert not [c for c in calls(env) if c.startswith(("docker rm", "docker stop"))]
    assert not [c for c in calls(env) if "git pull" in c]


def test_a_fresh_backup_comes_first_and_the_doctor_brackets_the_work(
    env: dict[str, str],
) -> None:
    update(env)
    log = calls(env)
    backup = log.index("backup-runner")
    upgrade = next(i for i, c in enumerate(log) if c.startswith("apt-get -y"))
    bot = next(
        i for i, c in enumerate(log) if c.startswith("bot-updater") and "--apply" in c
    )
    web = next(
        i for i, c in enumerate(log) if c.startswith("web-updater") and "--apply" in c
    )
    assert backup < upgrade < bot < web
    assert doctor_calls(env) == 2
    first = log.index("bot-updater --doctor")
    last = len(log) - 1 - log[::-1].index("bot-updater --doctor")
    assert first < backup  # the doctor looks before anything is changed
    assert last > web  # and again afterwards


def test_the_owner_gets_one_line_and_the_record_is_written(
    env: dict[str, str],
) -> None:
    update(env)
    (note,) = texts(env)
    assert note.startswith("🔄 Ежемесячное обновление сервера выполнено.")
    assert "Пакетов системы обновлено: 2" in note
    assert "пересобраны на свежих базовых образах" in note
    assert "Docker и его среда не обновляются автоматически" in note
    assert "docker-ce containerd.io" in note
    assert "перезагрузка" not in note
    saved = record(env)
    assert saved["result"] == "ok"
    assert saved["packages"] == "2"
    assert saved["images"] == "rebuilt"
    assert saved["reboot"] == "no"
    assert saved["last_ok"] == saved["time"]


def test_without_a_new_base_image_the_bot_and_the_site_are_not_touched(
    env: dict[str, str],
) -> None:
    change_state(env, pull_changes=[])
    result = update(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "images=unchanged" in result.stdout
    assert applied(env) == []
    assert "Базовые образы не изменились" in texts(env)[0]


def test_new_code_in_the_folder_is_never_deployed_by_the_update(
    env: dict[str, str],
) -> None:
    result = update({**env, "FAKE_FOLDER_DIGEST": "def456"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "images=skipped" in result.stdout
    assert "not deployed yet" in result.stdout
    assert applied(env) == []
    assert "ещё не выкатывали" in texts(env)[0]
    assert record(env)["result"] == "ok"  # the packages did go in


@pytest.mark.parametrize(
    ("digest", "label"),
    [("", "could not be computed"), ("abc123", "no source stamp")],
)
def test_an_unknown_or_unstamped_code_is_not_rebuilt_either(
    env: dict[str, str], digest: str, label: str
) -> None:
    if label == "no source stamp":
        change_container(env, "node-rental-bot-1", source="")
    result = update({**env, "FAKE_FOLDER_DIGEST": digest})
    assert label in result.stdout
    assert "images=skipped" in result.stdout
    assert applied(env) == []


def test_the_second_stamp_matters_too(env: dict[str, str]) -> None:
    change_container(env, "node-rental-web-local-app-web-1", source="old")
    result = update(env)
    assert "images=skipped" in result.stdout


def test_nothing_to_upgrade_is_fine(env: dict[str, str]) -> None:
    result = update({**env, "FAKE_UPGRADABLE": ""})
    assert "packages=0" in result.stdout
    assert installed(env) == []
    assert record(env)["result"] == "ok"


def test_only_docker_to_upgrade_means_nothing_is_installed(env: dict[str, str]) -> None:
    result = update({**env, "FAKE_UPGRADABLE": "docker-ce docker-ce-cli runc"})
    assert "packages=0" in result.stdout
    assert installed(env) == []
    assert "held back for you: docker-ce" in result.stdout


def test_an_upgrade_that_would_remove_a_package_is_not_made(
    env: dict[str, str],
) -> None:
    result = update({**env, "FAKE_APT_REMOVES": "libold1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert installed(env) == []
    assert "would remove packages (libold1" in result.stdout
    assert "обновление удалило бы пакеты (libold1" in texts(env)[0]
    assert "images=rebuilt" in result.stdout  # the rest of the update goes on


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        ({"FAKE_DPKG_AUDIT": "libfoo half-configured"}, "half-configured"),
        ({"FAKE_APT_UPDATE_FAIL": "1"}, "could not be refreshed"),
        ({"FAKE_APT_SIM_FAIL": "1"}, "trial run failed"),
    ],
)
def test_a_package_system_that_is_not_in_order_is_left_alone(
    env: dict[str, str], extra: dict[str, str], said: str
) -> None:
    result = update({**env, **extra})
    assert result.returncode == 0, result.stdout + result.stderr
    assert said in result.stdout
    assert installed(env) == []
    assert "Пакеты системы не обновлялись" in texts(env)[0]


def test_a_failed_upgrade_stops_everything_and_says_so(env: dict[str, str]) -> None:
    result = update({**env, "FAKE_APT_FAIL": "1"})
    assert result.returncode == 1
    assert "AUTO_UPDATE_FAILED: apt upgrade failed" in result.stderr
    assert "docker pull python:3.11-slim-bookworm" not in calls(env)
    assert applied(env) == []
    assert record(env)["result"] == "failed"
    assert "apt не справился" in texts(env)[0]


def test_a_container_that_does_not_come_back_after_the_upgrade_is_reported(
    env: dict[str, str],
) -> None:
    change_container(env, "marzban-marzban-1", stuck=True)
    result = update({**env, "FAKE_APT_STOPS": "marzban-marzban-1"})
    assert result.returncode == 1
    assert "not back: marzban-marzban-1" in result.stdout
    assert "docker start marzban-marzban-1" in calls(env)
    assert "AUTO_UPDATE_FAILED: containers are not back" in result.stderr
    assert "docker pull python:3.11-slim-bookworm" not in calls(env)
    assert "не поднялась" in texts(env)[0]


def test_a_container_that_stopped_is_started_again_and_the_update_goes_on(
    env: dict[str, str],
) -> None:
    result = update({**env, "FAKE_APT_STOPS": "marzban-marzban-1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docker start marzban-marzban-1" in calls(env)
    assert "AUTO_UPDATE_OK" in result.stdout


def test_a_pending_reboot_is_reported_and_never_done(env: dict[str, str]) -> None:
    result = update({**env, "FAKE_APT_NEEDS_REBOOT": "1"})
    assert "reboot=yes" in result.stdout
    assert "Нужна перезагрузка" in texts(env)[0]
    assert "reboot=yes" in Path(env["STATE_DIR"], "last-run").read_text("utf-8")
    assert not [c for c in calls(env) if "reboot" in c.split()[0:2] or c == "reboot"]


# ---------------------------------------------------- when the rebuild goes wrong
def test_a_bot_update_that_fails_stops_before_the_site(env: dict[str, str]) -> None:
    result = update({**env, "FAKE_BOT_FAIL": "1"})
    assert result.returncode == 1
    assert "AUTO_UPDATE_FAILED: the bot update did not finish" in result.stderr
    assert not [c for c in calls(env) if c.startswith("web-updater")]
    assert "Пакетов системы обновлено: 2" in texts(env)[0]
    assert record(env)["result"] == "failed"


def test_a_site_update_that_fails_is_reported(env: dict[str, str]) -> None:
    result = update({**env, "FAKE_WEB_FAIL": "1"})
    assert result.returncode == 1
    assert "AUTO_UPDATE_FAILED: the site update did not finish" in result.stderr
    assert "Бот уже обновлён" in texts(env)[0]


def test_new_problems_after_the_rebuild_send_the_images_back(
    env: dict[str, str],
) -> None:
    result = update({**env, "FAKE_BREAK_AFTER_BOT": "1"})
    assert result.returncode == 1
    assert "going back" in result.stdout or "go back" in result.stdout
    log = calls(env)
    assert "web-updater --rollback node-rental-web:rollback-T2" in log
    assert "bot-updater --rollback node-rental-bot:rollback-T1" in log
    assert "the doctor is clean again" in result.stderr
    assert record(env)["result"] == "failed"
    assert record(env)["images"] == "rolled-back"
    (note,) = texts(env)
    assert "возвращены на прежние образы" in note
    assert "healthz" in note  # what the doctor found


def test_a_rollback_that_does_not_help_says_the_packages_are_to_blame(
    env: dict[str, str],
) -> None:
    result = update({**env, "FAKE_BREAK_AFTER_BOT": "1", "FAKE_ROLLBACK_USELESS": "1"})
    assert result.returncode == 1
    assert "the doctor still finds problems" in result.stderr
    assert "возврат образов не помог" in texts(env)[0]


def test_new_problems_after_packages_alone_are_reported_without_a_rollback(
    env: dict[str, str],
) -> None:
    change_state(env, pull_changes=[])
    result = update({**env, "FAKE_APT_BREAKS_SITE": "1"})
    assert result.returncode == 1
    assert "new problems after the package upgrade" in result.stderr
    assert not [c for c in calls(env) if "--rollback" in c]
    assert "apt-versions-" in texts(env)[0]


# ------------------------------------------------------- what stops it quietly
def assert_skipped(
    env: dict[str, str], result: subprocess.CompletedProcess[str], reason: str
) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"AUTO_UPDATE_SKIPPED: {reason}" in result.stdout
    assert changes(env) == []
    assert record(env)["result"] == "skipped"


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (
            {"FAKE_BUSY": "update"},
            "an update, a backup, a cleanup or another maintenance script is running",
        ),
        ({"FAKE_BUSY": "apt"}, "the package manager is running"),
        ({"FAKE_AVAIL_MB": "300"}, "only 300 MB of memory available, 700 MB wanted"),
    ],
)
def test_it_waits_quietly_when_the_server_is_busy_or_short_of_memory(
    env: dict[str, str], extra: dict[str, str], reason: str
) -> None:
    result = update({**env, **extra})
    assert_skipped(env, result, reason)
    assert texts(env) == []


def test_it_waits_outside_the_quiet_hours(env: dict[str, str]) -> None:
    hour = datetime.now(UTC).hour
    start = (hour + 2) % 24
    window = {"WINDOW_FROM_UTC": str(start), "WINDOW_TO_UTC": str(start + 1)}
    result = update({**env, **window})
    assert_skipped(
        env, result, f"outside the quiet hours ({start}:00-{start + 1}:00 UTC)"
    )
    assert texts(env) == []


def test_it_waits_while_a_container_is_unwell_or_the_bot_is_down(
    env: dict[str, str],
) -> None:
    change_container(env, "marzban-marzban-1", status="Up 5 minutes (unhealthy)")
    result = update(env)
    assert_skipped(env, result, "a container is not healthy: marzban-marzban-1")

    change_container(env, "marzban-marzban-1", status="Up 5 minutes (healthy)")
    change_container(env, "node-rental-bot-1", status="Up 4 seconds (health: starting)")
    assert "not healthy: node-rental-bot-1" in update(env).stdout

    change_container(env, "node-rental-bot-1", status="Up 2 hours (healthy)")
    change_container(env, "node-rental-bot-1", running=False)
    assert_skipped(env, update(env), "the bot container is not running")


def test_it_waits_when_docker_does_not_answer(env: dict[str, str]) -> None:
    change_state(env, down=True)
    result = update(env)
    assert "AUTO_UPDATE_SKIPPED: docker is not answering" in result.stdout
    assert result.returncode == 0


def test_it_waits_while_another_update_holds_the_lock(env: dict[str, str]) -> None:
    lock = Path(env["LOCK_FILE"])
    lock.parent.mkdir(parents=True)
    with lock.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = update(env)
    assert "AUTO_UPDATE_SKIPPED: another update is still running" in result.stdout
    assert changes(env) == []
    assert not (Path(env["STATE_DIR"]) / "last-run").exists()  # the other run's record


def test_it_waits_while_a_real_cleanup_script_is_running(
    env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "bin" / "pgrep").unlink()  # the real pgrep, a real process
    script = tmp_path / "server_cleanup.sh"
    script.write_text("sleep 30\n", encoding="utf-8")
    process = subprocess.Popen(["bash", str(script)])  # noqa: S603, S607
    try:
        time.sleep(0.5)
        result = update(env)
    finally:
        process.kill()
        process.wait()
    assert "AUTO_UPDATE_SKIPPED: an update, a backup, a cleanup" in result.stdout
    assert changes(env) == []


def named_process(tmp_path: Path, name: str, *args: str) -> subprocess.Popen[bytes]:
    """A real process with the name and command line of a system tool."""
    folder = tmp_path / "named"
    folder.mkdir(exist_ok=True)
    script = folder / name
    script.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    return subprocess.Popen([str(script), *args])  # noqa: S603


def test_the_resident_upgrade_helper_is_not_a_running_upgrade(
    env: dict[str, str], tmp_path: Path
) -> None:
    # Ubuntu keeps unattended-upgrade-shutdown running all the time, and the kernel
    # cuts its process name to "unattended-upgr", the same as a real upgrade's.
    (tmp_path / "bin" / "pgrep").unlink()  # the real pgrep, a real process
    helper = named_process(tmp_path, "unattended-upgrade-shutdown", "--wait-for-signal")
    try:
        time.sleep(0.5)
        command = ["pgrep", "-x", "unattended-upgr"]
        by_name = subprocess.run(command, capture_output=True, check=False)  # noqa: S603
        assert by_name.returncode == 0  # a check by that name would have matched it
        result = update(env)
    finally:
        helper.kill()
        helper.wait()
    assert "AUTO_UPDATE_SKIPPED" not in result.stdout, result.stdout
    assert "AUTO_UPDATE_OK" in result.stdout


def test_a_running_upgrade_still_makes_it_wait(
    env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "bin" / "pgrep").unlink()  # the real pgrep, a real process
    upgrade = named_process(tmp_path, "unattended-upgrade", "-d")
    try:
        time.sleep(0.5)
        result = update(env)
    finally:
        upgrade.kill()
        upgrade.wait()
    assert_skipped(env, result, "the package manager is running")
    assert changes(env) == []


# --------------------------------------------------- what stops it, and says so
@pytest.mark.parametrize(
    ("extra", "reason", "text"),
    [
        ({"FAKE_FREE_MB": "2000"}, "only 2000 MB free on /", "на диске свободно 2000"),
        (
            {"FAKE_BACKUP_FAIL": "1"},
            "the fresh backup failed",
            "резервная копия не сделана",
        ),
        (
            {"FAKE_DOCTOR": "bad"},
            "the doctor finds problems before the update",
            "до обновления",
        ),
    ],
)
def test_it_tells_the_owner_when_it_cannot_start_and_changes_nothing(
    env: dict[str, str], tmp_path: Path, extra: dict[str, str], reason: str, text: str
) -> None:
    if extra.pop("FAKE_DOCTOR", "") == "bad":
        (tmp_path / "doctor.txt").write_text("bad", encoding="utf-8")
    result = update({**env, **extra})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"AUTO_UPDATE_SKIPPED: {reason}" in result.stdout
    assert installed(env) == []
    assert "docker pull python:3.11-slim-bookworm" not in calls(env)
    assert applied(env) == []
    (alert,) = texts(env)
    assert alert.startswith("⚠️ Автообновление не запущено")
    assert text in alert
    assert alert.endswith("Сервер не менялся.")
    assert record(env)["result"] == "skipped"


def test_without_a_scheduled_backup_it_never_starts(
    env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "node-backup.env").unlink()
    result = update(env)
    assert "AUTO_UPDATE_SKIPPED: no scheduled backup" in result.stdout
    assert "backup-runner" not in calls(env)
    assert installed(env) == []
    assert "install_backup.sh" in texts(env)[0]


# ------------------------------------------------------------ the owner's note
@pytest.mark.parametrize(
    ("notify", "expected_ok", "expected_alert"),
    [("always", 1, 1), ("problems", 0, 1), ("never", 0, 0), ("surprise", 1, 1)],
)
def test_the_note_follows_the_setting(
    env: dict[str, str], notify: str, expected_ok: int, expected_alert: int
) -> None:
    update({**env, "NOTIFY": notify})
    assert len(texts(env)) == expected_ok
    Path(env["FAKE_SENT"]).unlink(missing_ok=True)
    update({**env, "NOTIFY": notify, "FAKE_APT_FAIL": "1"})
    assert len(texts(env)) == expected_alert


def test_a_skipped_month_keeps_the_time_of_the_last_complete_one(
    env: dict[str, str],
) -> None:
    update(env)
    complete = record(env)["last_ok"]
    update({**env, "FAKE_BUSY": "update"})
    saved = record(env)
    assert saved["result"] == "skipped"
    assert saved["last_ok"] == complete


@pytest.mark.parametrize("value", ["abc", "-5", ""])
def test_a_bad_setting_falls_back_to_the_safe_default(
    env: dict[str, str], value: str
) -> None:
    result = update({**env, "WANT_FREE_MB": value, "FAKE_FREE_MB": "2000"})
    assert "only 2000 MB free on /, 4096 MB wanted" in result.stdout
    assert changes(env) == []


# -------------------------------------------------------------------- dry run
def test_a_dry_run_shows_the_plan_and_changes_nothing(env: dict[str, str]) -> None:
    result = update(env, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "AUTO UPDATE DRY RUN" in out
    assert "would pull: python:3.11-slim-bookworm" in out
    assert "caddy:2.11.7-builder (a new compiler layer" in out
    assert "would upgrade 2 package(s)" in out
    assert "held back for you: docker-ce" in out
    assert "would make a fresh encrypted backup first" in out
    assert "code in the project folder = code that runs: yes" in out
    assert "AUTO_UPDATE_DRY_RUN: nothing blocks the update; nothing was changed." in out
    assert changes(env) == []
    assert texts(env) == []
    assert not (Path(env["STATE_DIR"]) / "last-run").exists()


def test_a_dry_run_lists_every_reason_the_real_run_would_stop(
    env: dict[str, str],
) -> None:
    change_container(env, "marzban-marzban-1", status="Up 5 minutes (unhealthy)")
    result = update({**env, "FAKE_FREE_MB": "2000"}, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BLOCKED: a container is not healthy: marzban-marzban-1" in result.stdout
    assert "BLOCKED: only 2000 MB free on /" in result.stdout
    assert "AUTO_UPDATE_DRY_RUN: the real run would stop now:" in result.stdout
    assert texts(env) == []
    assert changes(env) == []


def test_a_dry_run_ignores_the_quiet_hours_but_says_them(env: dict[str, str]) -> None:
    hour = datetime.now(UTC).hour
    start = (hour + 2) % 24
    window = {"WINDOW_FROM_UTC": str(start), "WINDOW_TO_UTC": str(start + 1)}
    result = update({**env, **window}, "--dry-run")
    assert "the real run starts only between" in result.stdout
    assert "nothing blocks the update" in result.stdout


@pytest.mark.parametrize("args", [["--bogus"], ["--dry-run", "x"], ["--apply"]])
def test_options_it_does_not_know_stop_before_anything_is_touched(
    env: dict[str, str], args: list[str]
) -> None:
    result = update(env, *args)
    assert result.returncode == 2
    assert "usage: server_auto_update.sh [--dry-run]" in result.stderr
    assert not calls(env)


# --------------------------------------------------------- what is held back
@pytest.mark.parametrize(
    "name",
    [
        "docker-ce",
        "docker-ce-cli",
        "docker-buildx-plugin",
        "docker-compose-plugin",
        "docker-ce-rootless-extras",
        "docker.io",
        "containerd.io",
        "containerd",
        "runc",
    ],
)
def test_the_docker_family_is_held_back(name: str) -> None:
    source = (SCRIPTS / "server_auto_update.sh").read_text(encoding="utf-8")
    held = re.search(r"^HELD_BACK='(.+)'$", source, re.M)
    assert held
    assert re.search(held.group(1), name)


@pytest.mark.parametrize(
    "name",
    [
        "openssl",
        "dockerfile-mode",
        "libcontainerd1",
        "runcimage",
        "linux-image-generic",
    ],
)
def test_other_packages_are_not(name: str) -> None:
    source = (SCRIPTS / "server_auto_update.sh").read_text(encoding="utf-8")
    held = re.search(r"^HELD_BACK='(.+)'$", source, re.M)
    assert held
    assert not re.search(held.group(1), name)


def test_the_busy_patterns_see_the_scripts_and_not_an_editor_or_itself() -> None:
    source = (SCRIPTS / "server_auto_update.sh").read_text(encoding="utf-8")
    busy = re.compile(re.search(r"^BUSY_PATTERN='(.+)'$", source, re.M)[1])  # type: ignore[index]
    for line in (
        "bash scripts/server_update.sh --apply",
        "bash scripts/update_bot.sh /root/vpn-src --apply",
        "/bin/bash ./scripts/update_web.sh /root/vpn-src --apply",
        "bash /root/vpn-src/scripts/backup_all.sh",
        "bash /root/vpn-src/scripts/server_cleanup.sh --auto",
        "bash scripts/restore_backup.sh backup.tar.gz.age",
        "bash scripts/ssh_hardening.sh --apply",
    ):
        assert busy.search(line), line
    for line in (
        "/bin/bash /root/vpn-src/scripts/server_auto_update.sh",  # itself
        "vim scripts/server_update.sh",
        "tail -f /var/log/backup_all.sh.log",
    ):
        assert not busy.search(line), line
    # The weekly cleanup, for its part, steps aside for a running auto-update.
    cleanup = (SCRIPTS / "server_cleanup.sh").read_text(encoding="utf-8")
    weekly = re.compile(re.search(r"^BUSY_PATTERN='(.+)'$", cleanup, re.M)[1])  # type: ignore[index]
    assert weekly.search("/bin/bash /root/vpn-src/scripts/server_auto_update.sh")


# ------------------------------------------------------------------ installer
def install(
    env: dict[str, str], tmp_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "node-backup.env").write_text("AGE_RECIPIENT=x\n", encoding="utf-8")
    return run_script("install_auto_update.sh", {**env, "ETC_DIR": str(etc)}, *args)


def units(tmp_path: Path) -> Path:
    return tmp_path / "etc" / "systemd" / "system"


def test_the_installer_writes_a_monthly_timer_and_shows_the_plan(
    env: dict[str, str], tmp_path: Path
) -> None:
    result = install(env, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AUTO_UPDATE_INSTALL_OK" in result.stdout
    assert "AUTO_UPDATE_DRY_RUN" in result.stdout  # it shows, it does not do
    assert changes(env) == []
    service = (units(tmp_path) / "node-update.service").read_text(encoding="utf-8")
    assert f"ExecStart=/bin/bash {ROOT}/scripts/server_auto_update.sh" in service
    assert "Environment=NOTIFY=always" in service
    assert "Type=oneshot" in service
    assert "TimeoutStartSec=2h" in service
    assert "Nice=10" in service
    timer = (units(tmp_path) / "node-update.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Sun *-*-1..7 05:00:00 UTC" in timer
    assert "RandomizedDelaySec=10min" in timer
    # A month the server was off is skipped, not made up at an odd hour.
    assert "Persistent=false" in timer
    assert "systemctl enable --now node-update.timer" in calls(env)


def test_the_installer_refuses_to_run_without_the_daily_backup(
    env: dict[str, str], tmp_path: Path
) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    result = run_script("install_auto_update.sh", {**env, "ETC_DIR": str(etc)})
    assert result.returncode == 1
    assert "set up the daily backup first" in result.stderr
    assert not units(tmp_path).exists()


def test_the_installer_can_stay_quiet_and_can_turn_it_off(
    env: dict[str, str], tmp_path: Path
) -> None:
    result = install(env, tmp_path, "--quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    service = (units(tmp_path) / "node-update.service").read_text(encoding="utf-8")
    assert "Environment=NOTIFY=problems" in service
    result = install(env, tmp_path, "--remove")
    assert result.returncode == 0, result.stderr
    assert "AUTO_UPDATE_REMOVED" in result.stdout
    assert not (units(tmp_path) / "node-update.timer").exists()
    assert "systemctl disable --now node-update.timer" in calls(env)


@pytest.mark.parametrize("args", [["--bogus"], ["--quiet", "--remove"], ["x"]])
def test_the_installer_rejects_options_it_does_not_know(
    env: dict[str, str], tmp_path: Path, args: list[str]
) -> None:
    result = install(env, tmp_path, *args)
    assert result.returncode == 1
    assert "usage: install_auto_update.sh" in result.stderr
    assert not units(tmp_path).exists()


def test_the_installer_refuses_a_folder_that_is_not_a_plain_path(
    env: dict[str, str], tmp_path: Path
) -> None:
    odd = tmp_path / "my project"
    (odd / "scripts").mkdir(parents=True)
    for name in ("install_auto_update.sh", "server_auto_update.sh"):
        shutil.copy(SCRIPTS / name, odd / "scripts" / name)
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "node-backup.env").write_text("x\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        ["bash", str(odd / "scripts" / "install_auto_update.sh")],  # noqa: S607
        env={**env, "ETC_DIR": str(etc)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "plain path" in result.stderr
    assert not units(tmp_path).exists()


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd")
@pytest.mark.parametrize(
    ("script", "marker"),
    [
        ("install_auto_update.sh", "OnCalendar="),
        ("install_cleanup.sh", "OnCalendar=Sun"),
    ],
)
def test_systemd_accepts_the_timer_calendars(script: str, marker: str) -> None:
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    expressions = re.findall(r"^OnCalendar=(.+)$", text, re.M)
    assert expressions
    for expression in expressions:
        result = subprocess.run(  # noqa: S603
            ["systemd-analyze", "calendar", expression],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_the_monthly_slot_follows_the_backup_and_the_weekly_cleanup() -> None:
    def start(script: str, pattern: str) -> int:
        text = (SCRIPTS / script).read_text(encoding="utf-8")
        match = re.search(pattern, text)
        assert match
        return int(match[1]) * 60 + int(match[2])

    backup = start("install_backup.sh", r"OnCalendar=\*-\*-\* (\d\d):(\d\d)") + 10
    cleanup = start("install_cleanup.sh", r"OnCalendar=Sun \*-\*-\* (\d\d):(\d\d)") + 15
    monthly = start(
        "install_auto_update.sh", r"OnCalendar=Sun \*-\*-1\.\.7 (\d\d):(\d\d)"
    )
    assert monthly >= backup + 60  # the nightly copy is long done
    assert monthly >= cleanup + 10  # and so is the weekly cleanup of that Sunday


# ------------------------------------------------------------- the doctor's line
def put_unit(tmp_path: Path) -> None:
    units_dir = tmp_path / "units"
    units_dir.mkdir(exist_ok=True)
    (units_dir / "node-update.timer").write_text("[Timer]\n", encoding="utf-8")


def put_record(env: dict[str, str], **fields: object) -> None:
    folder = Path(env["STATE_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "time": int(time.time()),
        "last_ok": int(time.time()),
        "result": "ok",
        "packages": 14,
        "images": "rebuilt",
        "reboot": "no",
        "note": "packages 14, images rebuilt",
    }
    values.update(fields)
    (folder / "last-run").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )


def status(env: dict[str, str], tmp_path: Path, **extra: str) -> str:
    result = run_script(
        "auto_update_status.sh",
        {**env, "UNIT_DIR": str(tmp_path / "units"), **extra},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def ago(days: int) -> int:
    return int(time.time()) - days * DAY


def test_status_without_the_timer_says_how_to_set_it_up(
    env: dict[str, str], tmp_path: Path
) -> None:
    line = status(env, tmp_path)
    assert line.startswith("WARN no monthly update")
    assert "install_auto_update.sh" in line


def test_status_checks_the_timer_and_waits_for_the_first_run(
    env: dict[str, str], tmp_path: Path
) -> None:
    put_unit(tmp_path)
    assert status({**env, "FAKE_TIMER_INACTIVE": "1"}, tmp_path).startswith(
        "WARN the monthly update timer is not active"
    )
    assert status(env, tmp_path).startswith("OK monthly update: set up, waiting")
    old = time.time() - 50 * DAY
    os.utime(tmp_path / "units" / "node-update.timer", (old, old))
    assert "has never run" in status(env, tmp_path)


UNREADABLE = "WARN the record of the monthly update is unreadable"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "OK monthly update: ran 0 d ago, 14 package(s), images rebuilt"),
        (
            {"time": ago(50), "last_ok": ago(50)},
            "WARN the monthly update has not run for 50 d",
        ),
        (
            {"result": "failed", "note": "apt upgrade failed"},
            "WARN the monthly update failed 0 d ago: apt upgrade failed",
        ),
        (
            {"result": "skipped", "note": "an update is running", "last_ok": ago(30)},
            "OK monthly update: skipped 0 d ago (an update is running); "
            "the last complete run was 30 d ago; it tries again next month",
        ),
        (
            {
                "result": "skipped",
                "note": "docker is not answering",
                "last_ok": ago(60),
            },
            "WARN the monthly update was skipped 0 d ago (docker is not answering) "
            "and the last complete run was 60 d ago",
        ),
        (
            {"result": "skipped", "note": "x", "last_ok": ""},
            "WARN the monthly update was skipped 0 d ago (x) "
            "and it has never completed",
        ),
        ({"time": "soon"}, UNREADABLE),
        ({"result": "mystery"}, UNREADABLE),
    ],
)
def test_status_reads_the_record_of_the_last_run(
    env: dict[str, str], tmp_path: Path, fields: dict[str, object], expected: str
) -> None:
    put_unit(tmp_path)
    put_record(env, **fields)
    assert status(env, tmp_path).startswith(expected)


def test_status_reminds_of_a_reboot_that_has_been_waiting_too_long(
    env: dict[str, str], tmp_path: Path
) -> None:
    put_unit(tmp_path)
    put_record(env)
    marker = tmp_path / "reboot-required"
    marker.write_text("", encoding="utf-8")
    line = status(env, tmp_path)
    assert line.startswith("OK monthly update: ran 0 d ago")
    assert line.endswith("; a reboot is waiting (0 d)")

    old = time.time() - 20 * DAY
    os.utime(marker, (old, old))
    line = status(env, tmp_path)
    assert line.startswith("WARN a reboot has been waiting for 20 d")
    assert "reboot at a quiet hour" in line.replace("run reboot at", "reboot at")

    # Without the timer the reminder still comes: it is about the server.
    shutil.rmtree(tmp_path / "units")
    assert status(env, tmp_path).startswith("WARN a reboot has been waiting for 20 d")


def test_the_doctor_reads_what_the_update_wrote(
    env: dict[str, str], tmp_path: Path
) -> None:
    put_unit(tmp_path)
    assert update(env).returncode == 0
    assert status(env, tmp_path).startswith(
        "OK monthly update: ran 0 d ago, 2 package(s), images rebuilt"
    )


def test_the_doctor_shows_the_auto_update_line() -> None:
    doctor = (SCRIPTS / "update_bot.sh").read_text(encoding="utf-8")
    assert "auto_update_status.sh" in doctor.split("--doctor", 3)[-1]


# ------------------------------------------------------------- the source digest
def digest(folder: Path) -> str:
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPTS / "source_digest.sh"), str(folder)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    for folder in ("app", "web/src", "docs", "other"):
        (root / folder).mkdir(parents=True)
    (root / "app" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (root / "web" / "package.json").write_text("{}", encoding="utf-8")
    (root / "web" / "src" / "a.ts").write_text("a", encoding="utf-8")
    (root / "docs" / "README.md").write_text("docs", encoding="utf-8")
    (root / "other" / "x.txt").write_text("x", encoding="utf-8")
    (root / "Dockerfile").write_text(
        "FROM python:3.11 AS builder\n"
        "COPY --chown=1000:1000 app ./app\n"
        "FROM python:3.11 AS final\n"
        "COPY --from=builder /wheels /wheels\n"
        "COPY web/package.json web/src \\\n"
        "  ./web/\n",
        encoding="utf-8",
    )
    return root


def test_the_digest_is_stable_and_does_not_depend_on_where_the_folder_is(
    project: Path, tmp_path: Path
) -> None:
    first = digest(project)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert digest(project) == first
    shutil.copytree(project, tmp_path / "elsewhere")
    assert digest(tmp_path / "elsewhere") == first


def test_the_digest_follows_what_the_images_are_built_from(project: Path) -> None:
    base = digest(project)
    # Not copied into any image: docs, other folders, build leftovers.
    (project / "docs" / "README.md").write_text("changed", encoding="utf-8")
    (project / "other" / "x.txt").write_text("changed", encoding="utf-8")
    (project / "web" / "node_modules").mkdir()
    (project / "web" / "node_modules" / "m.js").write_text("m", encoding="utf-8")
    (project / "app" / "__pycache__").mkdir()
    (project / "app" / "__pycache__" / "main.cpython-311.pyc").write_text(
        "c", encoding="utf-8"
    )
    assert digest(project) == base
    # Copied: a source file, a file named in a continued COPY line, a new file.
    for relative in ("app/main.py", "web/src/a.ts", "web/package.json"):
        path = project / relative
        before = path.read_text(encoding="utf-8")
        path.write_text(before + "\n# changed", encoding="utf-8")
        assert digest(project) != base, relative
        path.write_text(before, encoding="utf-8")
        assert digest(project) == base
    (project / "app" / "new.py").write_text("new", encoding="utf-8")
    assert digest(project) != base
    (project / "app" / "new.py").unlink()
    # The recipe itself counts.
    dockerfile = project / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text("utf-8") + "# a comment\n", "utf-8")
    assert digest(project) != base


def test_the_digest_needs_a_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPTS / "source_digest.sh"), str(tmp_path / "empty")],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "no Dockerfile" in result.stderr


def test_the_real_project_has_a_digest_that_ignores_docs_and_scripts_not_copied() -> (
    None
):
    first = digest(ROOT)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # The three scripts the bot image carries count; the rest do not.
    assert "scripts/send_backup.py" in dockerfile
    assert "scripts/server_auto_update.sh" not in dockerfile


def test_both_updaters_stamp_what_they_build_and_the_update_reads_the_stamp() -> None:
    label = "node.rental.source"
    for name in ("update_bot.sh", "update_web.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "source_digest.sh" in text
        assert f'--label "{label}=$' in text
    auto = (SCRIPTS / "server_auto_update.sh").read_text(encoding="utf-8")
    assert label in auto
