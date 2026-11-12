"""The weekly cleanup (server_cleanup.sh --auto), its installer and the doctor's line.

The scripts run for real, with a stand-in docker, df, pgrep and systemctl. What
is checked is what makes it safe to run when nobody is watching: it removes only
what is old and already replaced, it does nothing while the server is busy or
unwell, and it says so when something changed."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
OLD_KERNEL = "linux-image-6.8.0-40-generic"

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
if args[:1] == ["ps"]:
    if "status=exited" in args:
        for c in containers:
            if not c["running"]:
                print(f"{c['name']} ({c['image']}, Exited (0) 3 weeks ago)")
    elif "{{.Image}}" in fmt:
        print("\n".join(c["image"] for c in containers))
    else:
        for c in containers:
            if "-a" in args or c["running"]:
                print(f"{c['name']}|{c['status']}" if "Status" in fmt else c["name"])
elif args[:1] == ["inspect"]:
    if "working_dir" in fmt:
        print(state["workdir"])
    elif "LogPath" in fmt:
        print(state["logs"].get(args[-1], ""))
elif args[:1] == ["images"]:
    repo = args[-1] if not args[-1].startswith("{{") else None
    for image in state["images"]:
        if repo is None or image.split(":")[0] == repo:
            print(image)
elif args[:2] == ["image", "inspect"]:
    print(123 * 1048576)
elif args[:2] == ["volume", "ls"]:
    print("\n".join(state["volumes"]))
elif args[:1] == ["builder"]:
    gone = state.get("stop_on_prune")
    for c in containers:
        if c["name"] == gone:
            c["running"] = False
    json.dump(state, open(state_path, "w"))
elif args[:1] == ["exec"]:
    if args[3] == "scripts/send_backup.py":
        with open(os.environ["FAKE_SENT"], "a") as sent:
            sent.write(args[5] + "\n")
        print("SENT")
    else:
        print("rows ok")
"""

FAKE_DF = r"""#!/usr/bin/env bash
echo "df $*" >> "$FAKE_LOG"
if [[ "$*" == *-Pm* ]]; then
  removed=$(grep -c '^docker rmi' "$FAKE_LOG" || true)
  echo "Filesystem 1048576-blocks Used Available Capacity Mounted on"
  echo "/dev/vda1 15000 8000 $(( ${FAKE_FREE_MB:-9000} + removed * 10 )) 50% /"
else
  echo "/dev/vda1 15G 8G 7G 50% /"
fi
"""

FAKE_APT_GET = f"""#!/usr/bin/env bash
echo "apt-get $*" >> "$FAKE_LOG"
if [[ "$*" == "-s autoremove" ]]; then
  echo "Remv {OLD_KERNEL} [6.8.0-40.40]"
fi
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

DAY = 86400


def stamp(days_ago: float, suffix: str = "") -> str:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return when.strftime("%Y%m%dT%H%M%SZ") + suffix


def age(path: Path, days: float) -> None:
    moment = time.time() - days * DAY
    os.utime(path, (moment, moment))


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "docker": FAKE_DOCKER,
        "df": FAKE_DF,
        "apt-get": FAKE_APT_GET,
        "pgrep": FAKE_PGREP,
        "systemctl": FAKE_SYSTEMCTL,
        "journalctl": '#!/usr/bin/env bash\necho "journalctl $*" >> "$FAKE_LOG"\n',
    }.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)

    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    (project / "secrets").mkdir()
    # Copies left by updates: the newest two stay, so do all that are young.
    for name, days in {"a": 30, "b": 20, "c": 10, "d": 5, "e": 3, "f": 1}.items():
        copy = project / "data" / f"before-update-{name}.db"
        copy.write_text("copy", encoding="utf-8")
        age(copy, days)
    for name, days in {"a": 30, "b": 10, "c": 5, "d": 1}.items():
        copy = project / f".env.before-update-{name}"
        copy.write_text("SECRET=1", encoding="utf-8")
        age(copy, days)
    old_key = project / "secrets" / "db_key.bak-old"
    old_key.write_text("key", encoding="utf-8")
    age(old_key, 30)

    home = tmp_path / "home"
    (home / "vpn-update").mkdir(parents=True)
    big_log = tmp_path / "marzban-json.log"
    with big_log.open("wb") as handle:  # sparse: 250 MB on paper, nothing on disk
        handle.truncate(250 * 1024 * 1024)

    up = "Up 2 hours (healthy)"
    state = {
        "workdir": str(project),
        "containers": [
            {"name": "node-rental-bot-1", "image": "node-rental-bot:latest"},
            {
                "name": "node-rental-web-local-app-web-1",
                "image": f"node-rental-web:release-{stamp(30)}",
            },
            {"name": "marzban-marzban-1", "image": "gozargah/marzban:v0.8.4"},
        ],
        "images": [
            "node-rental-bot:latest",
            f"node-rental-bot:rollback-{stamp(20, '-1')}",
            f"node-rental-bot:rollback-{stamp(15, '-2')}",
            f"node-rental-bot:rollback-{stamp(10, '-3')}",
            f"node-rental-bot:rollback-{stamp(2, '-4')}",
            f"node-rental-bot:update-{stamp(12, '-5')}",
            f"node-rental-bot:update-{stamp(2, '-6')}",
            "node-rental-bot:handmade",
            "node-rental-web:latest",
            f"node-rental-web:release-{stamp(30)}",
            f"node-rental-web:rollback-{stamp(50)}",
            f"node-rental-web:rollback-{stamp(45)}",
            f"node-rental-web:rollback-{stamp(40)}",
            "gozargah/marzban:v0.8.4",
            "gozargah/marzban:v0.8.3",
            "hello-world:latest",
        ],
        "volumes": ["3f9c0ffee"],
        "logs": {"marzban-marzban-1": str(big_log)},
    }
    for container in state["containers"]:
        container.update({"running": True, "status": up})
    state["containers"].append(
        {
            "name": "old-tunnel",
            "image": "cloudflare/cloudflared:2024.1.0",
            "running": False,
            "status": "Exited (0) 3 weeks ago",
        }
    )
    (tmp_path / "docker.json").write_text(json.dumps(state), encoding="utf-8")
    return {
        **{k: v for k, v in os.environ.items() if k not in {"NOTIFY", "FAKE_BUSY"}},
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(tmp_path / "docker.json"),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_SENT": str(tmp_path / "sent.txt"),
        "BIG_LOG": str(big_log),
        "CLEANUP_HOME": str(home),
        "STATE_DIR": str(tmp_path / "state"),
        "LOCK_FILE": str(tmp_path / "run" / "cleanup.lock"),
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


def auto(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return run_script("server_cleanup.sh", env, "--auto", *args)


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def texts(env: dict[str, str]) -> list[str]:
    sent = Path(env["FAKE_SENT"])
    return sent.read_text(encoding="utf-8").splitlines() if sent.exists() else []


def removed_images(env: dict[str, str]) -> list[str]:
    return [c.split()[-1] for c in calls(env) if c.startswith("docker rmi")]


def record(env: dict[str, str]) -> dict[str, str]:
    path = Path(env["STATE_DIR"]) / "last-run"
    pairs = (line.split("=", 1) for line in path.read_text("utf-8").splitlines())
    return dict(pairs)


def change_state(env: dict[str, str], **changes: object) -> None:
    path = Path(env["FAKE_DOCKER_STATE"])
    state = json.loads(path.read_text(encoding="utf-8"))
    state.update(changes)
    path.write_text(json.dumps(state), encoding="utf-8")


def untouched(env: dict[str, str]) -> bool:
    destructive = (
        "docker rmi",
        "docker image prune",
        "docker builder",
        "journalctl --vacuum",
        "apt-get clean",
        "apt-get -y",
    )
    return (
        not [c for c in calls(env) if c.startswith(destructive)]
        and Path(env["BIG_LOG"]).stat().st_size == 250 * 1024 * 1024
    )


# ------------------------------------------------------------ what it removes
def test_the_weekly_run_removes_only_what_is_old_and_replaced(
    env: dict[str, str],
) -> None:
    result = auto(env)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout

    # Rollback images: the newest two stay, so does everything younger than a week
    # and anything without a time in its tag. An image a container runs is never
    # removed, nor is :latest.
    names = {re.sub(r"-\d{8}T\d{6}Z.*", "", t) for t in removed_images(env)}
    assert names == {
        "node-rental-bot:rollback",
        "node-rental-bot:update",
        "node-rental-web:rollback",
    }
    assert len(removed_images(env)) == 4
    kept = " ".join(out.splitlines())
    for tag in ("node-rental-bot:handmade", "node-rental-bot:latest"):
        assert tag not in " ".join(removed_images(env))
    assert "keep: node-rental-bot:handmade (younger than 7 days, or its age" in kept
    assert "node-rental-web:release" not in " ".join(removed_images(env))
    assert not [t for t in removed_images(env) if "gozargah" in t]

    # Dangling images and build cache: only what is old, never what was just built.
    assert "docker image prune -f --filter until=168h" in calls(env)
    assert "docker builder prune -f --reserved-space 2GB --filter until=72h" in calls(
        env
    )

    # Copies: the newest two and every copy under a week old stay.
    data = Path(env["STATE_DIR"]).parent / "project" / "data"
    assert sorted(p.name for p in data.glob("before-*.db")) == [
        "before-update-d.db",
        "before-update-e.db",
        "before-update-f.db",
    ]
    project = data.parent
    assert sorted(p.name for p in project.glob(".env.before-*")) == [
        ".env.before-update-c",
        ".env.before-update-d",
    ]
    assert (project / "secrets" / "db_key.bak-old").exists()
    assert (Path(env["CLEANUP_HOME"]) / "vpn-update").exists()
    assert "left for the supervised cleanup" in out

    # Logs and the journal are the safe housekeeping of every week.
    assert Path(env["BIG_LOG"]).stat().st_size == 0
    assert "journalctl --vacuum-time=14d" in calls(env)
    assert "apt-get clean" in calls(env)


def test_the_weekly_run_never_touches_what_needs_someone_at_the_keyboard(
    env: dict[str, str],
) -> None:
    result = auto(env)
    assert result.returncode == 0, result.stdout + result.stderr
    everything = calls(env)
    # Kernels, the bot's database, other versions of images, stopped containers
    # and volumes are left to a supervised cleanup.
    assert not [c for c in everything if "purge" in c]
    assert f"old kernels are left for the supervised cleanup: {OLD_KERNEL}" in (
        result.stdout
    )
    assert not [c for c in everything if "db_maintenance" in c]
    assert "gozargah/marzban:v0.8.3" not in " ".join(removed_images(env))
    removals = ("docker rm ", "docker volume rm")
    assert not [c for c in everything if c.startswith(removals)]
    assert "hello-world:latest" in result.stdout  # listed
    assert "old-tunnel" in result.stdout
    assert "3f9c0ffee" in result.stdout


def test_the_supervised_run_is_unchanged_and_leaves_no_record(
    env: dict[str, str],
) -> None:
    result = run_script("server_cleanup.sh", env, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    # Not the weekly rules: the old v0.8.3 image, the kernel and the key go too.
    assert "gozargah/marzban:v0.8.3" in removed_images(env)
    assert [c for c in calls(env) if "purge" in c] == [
        f"apt-get -y -q purge {OLD_KERNEL}"
    ]
    assert not (Path(env["CLEANUP_HOME"]) / "vpn-update").exists()
    assert not (Path(env["STATE_DIR"]) / "last-run").exists()
    assert texts(env) == []


# ------------------------------------------------------- what stops it quietly
def assert_skipped(
    env: dict[str, str], result: subprocess.CompletedProcess[str], reason: str
) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"CLEANUP_AUTO_SKIPPED: {reason}" in result.stdout
    assert untouched(env)
    assert record(env)["result"] == "skipped"
    assert texts(env) == []


@pytest.mark.parametrize(
    ("busy", "reason"),
    [
        ("update", "an update, a backup or another maintenance script is running"),
        ("apt", "the package manager is running"),
    ],
)
def test_it_waits_while_an_update_or_the_package_manager_runs(
    env: dict[str, str], busy: str, reason: str
) -> None:
    result = auto({**env, "FAKE_BUSY": busy})
    assert_skipped(env, result, reason)


def test_it_waits_while_a_real_update_script_is_running(
    env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "bin" / "pgrep").unlink()  # the real pgrep, a real process
    script = tmp_path / "update_bot.sh"
    script.write_text("sleep 30\n", encoding="utf-8")
    process = subprocess.Popen(["bash", str(script)])  # noqa: S603, S607
    try:
        time.sleep(0.5)
        result = auto(env)
    finally:
        process.kill()
        process.wait()
    assert_skipped(
        env, result, "an update, a backup or another maintenance script is running"
    )


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
        result = auto(env)
    finally:
        helper.kill()
        helper.wait()
    assert "CLEANUP_AUTO_SKIPPED" not in result.stdout, result.stdout
    assert "CLEANUP_AUTO_OK" in result.stdout


def test_a_running_upgrade_still_makes_it_wait(
    env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "bin" / "pgrep").unlink()  # the real pgrep, a real process
    upgrade = named_process(tmp_path, "unattended-upgrade", "-d")
    try:
        time.sleep(0.5)
        result = auto(env)
    finally:
        upgrade.kill()
        upgrade.wait()
    assert_skipped(env, result, "the package manager is running")


def test_the_busy_pattern_matches_the_scripts_and_not_an_editor_or_itself() -> None:
    source = (SCRIPTS / "server_cleanup.sh").read_text(encoding="utf-8")
    pattern = re.search(r"^BUSY_PATTERN='(.+)'$", source, re.M)
    assert pattern
    busy = re.compile(pattern.group(1))
    for line in (
        "bash scripts/update_bot.sh /root/vpn-src --apply",
        "/bin/bash ./scripts/server_update.sh --apply",
        "bash /root/vpn-src/scripts/backup_all.sh",
        "bash /root/vpn-src/scripts/update_web.sh /root/vpn-src --apply",
        "bash scripts/restore_backup.sh backup.tar.gz.age",
        "bash scripts/ssh_hardening.sh --apply",
    ):
        assert busy.search(line), line
    for line in (
        "vim scripts/update_bot.sh",
        "less /root/vpn-src/scripts/server_update.sh",
        "tail -f /var/log/backup_all.sh.log",
        "bash /root/vpn-src/scripts/server_cleanup.sh --auto",
        "/bin/bash /root/vpn-src/scripts/install_cleanup.sh",
    ):
        assert not busy.search(line), line


def test_it_waits_while_another_cleanup_holds_the_lock(env: dict[str, str]) -> None:
    lock = Path(env["LOCK_FILE"])
    lock.parent.mkdir(parents=True)
    with lock.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = auto(env)
    assert_skipped(env, result, "another cleanup is still running")


def test_it_leaves_everything_alone_while_a_container_is_unwell(
    env: dict[str, str],
) -> None:
    state = json.loads(Path(env["FAKE_DOCKER_STATE"]).read_text(encoding="utf-8"))
    state["containers"][2]["status"] = "Up 5 minutes (unhealthy)"
    change_state(env, containers=state["containers"])
    result = auto(env)
    assert_skipped(
        env,
        result,
        "not touching anything while a container is not healthy: marzban-marzban-1",
    )


def test_it_waits_for_a_container_that_is_still_starting(env: dict[str, str]) -> None:
    state = json.loads(Path(env["FAKE_DOCKER_STATE"]).read_text(encoding="utf-8"))
    state["containers"][0]["status"] = "Up 8 seconds (health: starting)"
    change_state(env, containers=state["containers"])
    result = auto(env)
    assert "not healthy: node-rental-bot-1" in result.stdout
    assert untouched(env)


def test_it_does_nothing_when_docker_or_the_bot_is_not_running(
    env: dict[str, str],
) -> None:
    state = json.loads(Path(env["FAKE_DOCKER_STATE"]).read_text(encoding="utf-8"))
    state["containers"][0]["running"] = False
    change_state(env, containers=state["containers"])
    assert_skipped(env, auto(env), "the bot container is not running")

    change_state(env, down=True)
    result = auto(env)
    assert "CLEANUP_AUTO_SKIPPED: docker is not answering" in result.stdout
    assert result.returncode == 0


# ------------------------------------------------- what is reported afterwards
def test_a_container_that_goes_away_during_the_cleanup_is_reported(
    env: dict[str, str],
) -> None:
    change_state(env, stop_on_prune="marzban-marzban-1")
    result = auto(env)
    assert result.returncode == 1
    assert "CLEANUP_AUTO_FAILED: the running containers changed" in result.stderr
    assert "marzban-marzban-1" in result.stderr
    assert record(env)["result"] == "failed"
    (message,) = texts(env)
    assert message.startswith("⚠️ Автоочистка закончилась")
    assert "набор запущенных контейнеров изменился" in message


def test_a_missing_bot_container_stops_with_an_error(env: dict[str, str]) -> None:
    change_state(env, workdir="")
    result = auto(env)
    assert result.returncode == 1
    assert "CLEANUP_AUTO_FAILED: bot container node-rental-bot-1 not found" in (
        result.stderr
    )
    assert record(env)["result"] == "failed"
    assert untouched(env)


def test_the_owner_gets_one_short_note_and_the_record_is_written(
    env: dict[str, str],
) -> None:
    result = auto(env)
    # 4 images, 3 copies of the database and 2 copies of .env
    assert "CLEANUP_AUTO_OK removed=9 freed=40MB free=8.8GB" in result.stdout
    (note,) = texts(env)
    assert note.startswith("🧹 Еженедельная очистка сервера: освобождено 40 МБ")
    assert "свободно 8.8 ГБ" in note
    # Nothing is removed from this list: it is only what is left to the owner.
    assert "остановленных контейнеров: 1, томов без контейнера: 1" in note
    assert "образов без контейнера: 2" in note
    saved = record(env)
    assert saved["result"] == "ok"
    assert saved["freed_mb"] == "40"
    assert saved["manual"] == "4"
    assert saved["last_ok"] == saved["time"]

    # The same items next week are not mentioned again.
    auto(env)
    assert len(texts(env)) == 2
    assert "решение за вами" not in texts(env)[1]


def test_a_skipped_run_keeps_the_time_of_the_last_complete_one(
    env: dict[str, str],
) -> None:
    auto(env)
    complete = record(env)["last_ok"]
    assert complete
    auto({**env, "FAKE_BUSY": "update"})
    saved = record(env)
    assert saved["result"] == "skipped"
    assert saved["last_ok"] == complete


@pytest.mark.parametrize(
    ("notify", "free", "expected"),
    [
        ("always", "9000", 1),
        ("problems", "9000", 0),
        ("never", "9000", 0),
        ("problems", "1000", 1),
        ("always", "1000", 1),
        ("never", "1000", 0),
        ("surprise", "9000", 1),  # an unknown value never silences the note
    ],
)
def test_the_note_follows_the_setting_and_low_space_is_a_problem(
    env: dict[str, str], notify: str, free: str, expected: int
) -> None:
    result = auto({**env, "NOTIFY": notify, "FAKE_FREE_MB": free})
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(texts(env)) == expected
    if free == "1000" and expected:
        assert "мало места" in texts(env)[0]
        assert "свободно 1.0 ГБ" in texts(env)[0]


def test_a_dry_run_shows_the_plan_and_changes_nothing(env: dict[str, str]) -> None:
    result = auto(env, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AUTO DRY RUN" in result.stdout
    assert "CLEANUP_AUTO_DRY_RUN" in result.stdout
    assert re.search(r"would remove: node-rental-bot:rollback-\d{8}T", result.stdout)
    assert "keep: node-rental-bot:handmade (younger than 7 days" in result.stdout
    assert untouched(env)
    assert not (Path(env["STATE_DIR"]) / "last-run").exists()
    assert texts(env) == []
    assert (
        len(list((Path(env["STATE_DIR"]).parent / "project" / "data").glob("*"))) == 6
    )


def test_a_dry_run_also_says_why_the_real_run_would_wait(env: dict[str, str]) -> None:
    result = auto({**env, "FAKE_BUSY": "update"}, "--dry-run")
    assert "CLEANUP_AUTO_SKIPPED" in result.stdout
    assert not (Path(env["STATE_DIR"]) / "last-run").exists()


@pytest.mark.parametrize(
    "args",
    [["--bogus"], ["--dry-run"], ["--auto", "--apply"], ["--apply", "--dry-run"]],
)
def test_options_it_does_not_know_stop_before_anything_is_touched(
    env: dict[str, str], args: list[str]
) -> None:
    result = run_script("server_cleanup.sh", env, *args)
    assert result.returncode == 2
    assert "usage: server_cleanup.sh" in result.stderr
    assert not calls(env)


@pytest.mark.parametrize("value", ["0", "-3", "week", ""])
def test_a_bad_age_setting_falls_back_to_a_week(
    env: dict[str, str], value: str
) -> None:
    result = auto({**env, "MIN_AGE_DAYS": value})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docker image prune -f --filter until=168h" in calls(env)


# -------------------------------------------------------------- the installer
def install(
    env: dict[str, str], tmp_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return run_script(
        "install_cleanup.sh", {**env, "ETC_DIR": str(tmp_path / "etc")}, *args
    )


def test_the_installer_writes_a_weekly_timer_and_makes_the_first_pass(
    env: dict[str, str], tmp_path: Path
) -> None:
    result = install(env, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLEANUP_AUTO_OK" in result.stdout  # the first pass ran
    assert "CLEANUP_INSTALL_OK" in result.stdout
    units = tmp_path / "etc" / "systemd" / "system"
    service = (units / "node-cleanup.service").read_text(encoding="utf-8")
    assert f"ExecStart=/bin/bash {ROOT}/scripts/server_cleanup.sh --auto" in service
    assert "Environment=NOTIFY=always" in service
    assert "Type=oneshot" in service
    assert "Nice=19" in service
    assert "IOSchedulingClass=idle" in service
    assert "TimeoutStartSec=30min" in service
    timer = (units / "node-cleanup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Sun *-*-* 04:30:00 UTC" in timer
    assert "RandomizedDelaySec=15min" in timer
    assert "Persistent=true" in timer
    assert "systemctl enable --now node-cleanup.timer" in calls(env)
    assert len(texts(env)) == 1  # the first pass told the owner


def test_the_weekly_slot_follows_the_daily_backup() -> None:
    backup = (SCRIPTS / "install_backup.sh").read_text(encoding="utf-8")
    cleanup = (SCRIPTS / "install_cleanup.sh").read_text(encoding="utf-8")
    start = re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d)", backup)
    weekly = re.search(r"OnCalendar=Sun \*-\*-\* (\d\d):(\d\d)", cleanup)
    assert start
    assert weekly
    gap = (int(weekly[1]) * 60 + int(weekly[2])) - (
        int(start[1]) * 60 + int(start[2]) + 10  # the backup's random delay
    )
    assert gap >= 30


def test_the_installer_can_stay_quiet_unless_something_is_wrong(
    env: dict[str, str], tmp_path: Path
) -> None:
    result = install(env, tmp_path, "--quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    service = tmp_path / "etc" / "systemd" / "system" / "node-cleanup.service"
    assert "Environment=NOTIFY=problems" in service.read_text(encoding="utf-8")
    assert texts(env) == []


def test_the_installer_can_turn_it_off_again(
    env: dict[str, str], tmp_path: Path
) -> None:
    install(env, tmp_path)
    units = tmp_path / "etc" / "systemd" / "system"
    assert (units / "node-cleanup.timer").exists()
    result = install(env, tmp_path, "--remove")
    assert result.returncode == 0, result.stderr
    assert "CLEANUP_REMOVED" in result.stdout
    assert not (units / "node-cleanup.timer").exists()
    assert not (units / "node-cleanup.service").exists()
    assert "systemctl disable --now node-cleanup.timer" in calls(env)


def test_a_failed_first_pass_is_not_hidden(env: dict[str, str], tmp_path: Path) -> None:
    change_state(env, workdir="")
    result = install(env, tmp_path)
    assert result.returncode == 1
    assert "CLEANUP_INSTALL_DONE_FIRST_PASS_FAILED" in result.stderr
    assert (tmp_path / "etc" / "systemd" / "system" / "node-cleanup.timer").exists()


@pytest.mark.parametrize("args", [["--bogus"], ["--quiet", "--remove"], ["x"]])
def test_the_installer_rejects_options_it_does_not_know(
    env: dict[str, str], tmp_path: Path, args: list[str]
) -> None:
    result = install(env, tmp_path, *args)
    assert result.returncode == 1
    assert "usage: install_cleanup.sh" in result.stderr
    assert not (tmp_path / "etc").exists()


def test_the_installer_refuses_a_folder_that_is_not_a_plain_path(
    env: dict[str, str], tmp_path: Path
) -> None:
    # The folder goes into a unit file: nothing with a space or a $ may get in.
    odd = tmp_path / "my project"
    (odd / "scripts").mkdir(parents=True)
    for name in ("install_cleanup.sh", "server_cleanup.sh"):
        shutil.copy(SCRIPTS / name, odd / "scripts" / name)
    result = subprocess.run(  # noqa: S603
        ["bash", str(odd / "scripts" / "install_cleanup.sh")],  # noqa: S607
        env={**env, "ETC_DIR": str(tmp_path / "etc")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "plain path" in result.stderr
    assert not (tmp_path / "etc").exists()


# ----------------------------------------------------------- the doctor's line
def put_unit(tmp_path: Path) -> Path:
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    (units / "node-cleanup.timer").write_text("[Timer]\n", encoding="utf-8")
    return units


def put_record(env: dict[str, str], **fields: object) -> None:
    folder = Path(env["STATE_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    values = {
        "time": int(time.time()),
        "last_ok": int(time.time()),
        "result": "ok",
        "freed_mb": 1900,
        "free_mb": 7200,
        "manual": 0,
        "note": "removed 4 item(s)",
    }
    values.update(fields)
    (folder / "last-run").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )


def status(env: dict[str, str], tmp_path: Path) -> str:
    result = run_script(
        "cleanup_status.sh", {**env, "UNIT_DIR": str(tmp_path / "units")}
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_status_without_the_timer_says_how_to_set_it_up(
    env: dict[str, str], tmp_path: Path
) -> None:
    line = status(env, tmp_path)
    assert line.startswith("WARN no weekly cleanup")
    assert "install_cleanup.sh" in line


def test_status_checks_the_timer_and_that_a_run_was_recorded(
    env: dict[str, str], tmp_path: Path
) -> None:
    put_unit(tmp_path)
    assert status({**env, "FAKE_TIMER_INACTIVE": "1"}, tmp_path).startswith(
        "WARN the weekly cleanup timer is not active"
    )
    assert "has not run yet" in status(env, tmp_path)


def ago(days: int) -> int:
    return int(time.time()) - days * DAY


UNREADABLE = "WARN the record of the weekly cleanup is unreadable"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "OK weekly cleanup: ran 0 d ago, freed 1900 MB, 7.0 GB free"),
        (
            {"time": ago(12), "last_ok": ago(12)},
            "WARN the weekly cleanup has not run for 12 d",
        ),
        (
            {"result": "failed", "note": "the lock file cannot be opened"},
            "WARN the weekly cleanup failed 0 d ago: the lock file cannot be opened",
        ),
        (
            {"result": "skipped", "note": "an update is running", "last_ok": ago(6)},
            "OK weekly cleanup: skipped 0 d ago (an update is running); "
            "the last complete run was 6 d ago",
        ),
        (
            {"result": "skipped", "note": "docker is down", "last_ok": ago(15)},
            "WARN the weekly cleanup was skipped 0 d ago (docker is down) "
            "and the last complete run was 15 d ago",
        ),
        (
            {"result": "skipped", "note": "x", "last_ok": ""},
            "WARN the weekly cleanup was skipped 0 d ago (x) "
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


def test_the_doctor_reads_what_the_weekly_run_wrote(
    env: dict[str, str], tmp_path: Path
) -> None:
    put_unit(tmp_path)
    assert auto(env).returncode == 0
    assert status(env, tmp_path).startswith(
        "OK weekly cleanup: ran 0 d ago, freed 40 MB"
    )


def test_the_doctor_shows_the_cleanup_line() -> None:
    doctor = (SCRIPTS / "update_bot.sh").read_text(encoding="utf-8")
    assert "cleanup_status.sh" in doctor.split("--doctor", 3)[-1]
