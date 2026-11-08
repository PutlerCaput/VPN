"""Encrypted backups: what is packed, that only the private key opens it, what
happens on every failure, the restore, the installer and the doctor's line.

The shell scripts run for real, with a stand-in docker and the real age."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tarfile
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import send_backup

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SECRET = "SUPER-SECRET-BOT-TOKEN-1234"

needs_age = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age is not installed",
)
needs_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="the installer refuses to run without root"
)

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
args=("$@")
case "${args[0]}" in
  inspect)
    [[ "${FAKE_NO_CONTAINER:-0}" == 1 ]] && exit 1
    if [[ "$*" == *working_dir* ]]; then echo "$FAKE_WORKDIR"; fi
    exit 0 ;;
  exec)
    script="${args[3]}"; kind="${args[4]}"
    case "$script" in
      scripts/db_tool.py)
        [[ "${FAKE_DB_FAIL:-0}" == 1 ]] && exit 1
        echo "BOT-DATABASE-CONTENT" > "$FAKE_WORKDIR/data/.nightly-backup.db"
        echo "backup written" ;;
      scripts/send_backup.py)
        if [[ "$kind" == file ]]; then
          [[ "${FAKE_SEND_FAIL:-0}" == 1 ]] && exit 1
          # The real bot is another user: it needs read access to the file.
          stat -c %a "$FAKE_WORKDIR/data/.outgoing-backup.age" > "$FAKE_SENT/mode.txt"
          cp "$FAKE_WORKDIR/data/.outgoing-backup.age" "$FAKE_SENT/sent.age"
          echo "${args[6]}" > "$FAKE_SENT/caption.txt"
        else
          echo "${args[5]}" >> "$FAKE_SENT/texts.log"
        fi
        echo SENT ;;
    esac ;;
esac
"""

FAKE_APT_GET = '#!/usr/bin/env bash\necho "apt-get $*" >> "$FAKE_LOG"\n'
FAKE_SYSTEMCTL = '#!/usr/bin/env bash\necho "systemctl $*" >> "$FAKE_LOG"\n'


def make_keys(folder: Path) -> tuple[str, Path]:
    identity = folder / "age-key.txt"
    result = subprocess.run(  # noqa: S603
        ["age-keygen", "-o", str(identity)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    public = (result.stdout + result.stderr).split("Public key:")[1].split()[0]
    return public, identity


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "docker": FAKE_DOCKER,
        "apt-get": FAKE_APT_GET,
        "systemctl": FAKE_SYSTEMCTL,
    }.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)

    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    (project / "secrets").mkdir()
    (project / ".env").write_text(f"BOT_TOKEN_NOTE={SECRET}\n", encoding="utf-8")
    (project / "secrets" / "db_key.txt").write_text("ab" * 32, encoding="utf-8")

    marzban = tmp_path / "opt-marzban"
    marzban.mkdir()
    (marzban / ".env").write_text("UVICORN_PORT=8000\n", encoding="utf-8")
    (marzban / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (marzban / "Caddyfile").write_text("example.org {}\n", encoding="utf-8")
    xray = tmp_path / "xray_config.json"
    xray.write_text("{}", encoding="utf-8")
    database = tmp_path / "marzban.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users (name) VALUES ('client-1')")

    sent = tmp_path / "sent"
    sent.mkdir()
    public = ""
    if shutil.which("age-keygen"):
        public, identity = make_keys(tmp_path)
    else:
        identity = tmp_path / "age-key.txt"
    return {
        **{k: v for k, v in os.environ.items() if k != "BACKUP_ENV"},
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_WORKDIR": str(project),
        "FAKE_SENT": str(sent),
        "AGE_RECIPIENT": public,
        "AGE_IDENTITY": str(identity),
        "BACKUP_ENV": str(tmp_path / "missing.env"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "MARZBAN_DB": str(database),
        "MARZBAN_DIR": str(marzban),
        "XRAY_CONFIG": str(xray),
    }


def run(
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


def archives(env: dict[str, str]) -> list[Path]:
    return sorted(Path(env["BACKUP_DIR"]).glob("backup-*.tar.gz.age"))


def open_archive(env: dict[str, str], archive: Path) -> tarfile.TarFile:
    decrypted = subprocess.run(  # noqa: S603
        ["age", "-d", "-i", env["AGE_IDENTITY"], str(archive)],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout
    return tarfile.open(fileobj=BytesIO(decrypted), mode="r:gz")


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# ------------------------------------------------------------------ the backup
@needs_age
def test_the_archive_holds_everything_and_only_the_private_key_opens_it(
    env: dict[str, str],
) -> None:
    result = run("backup_all.sh", env)
    assert result.returncode == 0, result.stderr
    assert "BACKUP_OK" in result.stdout
    assert "telegram=sent" in result.stdout

    (archive,) = archives(env)
    raw = archive.read_bytes()
    assert SECRET.encode() not in raw  # the public key only locks
    assert b"BOT-DATABASE-CONTENT" not in raw

    with open_archive(env, archive) as tar:
        names = set(tar.getnames())
        assert {
            "./bot/bot.db",
            "./bot/.env",
            "./bot/secrets/db_key.txt",
            "./marzban/db.sqlite3",
            "./marzban/.env",
            "./marzban/docker-compose.yml",
            "./marzban/Caddyfile",
            "./marzban/xray_config.json",
            "./MANIFEST.sha256",
            "./INFO.txt",
        } <= names
        env_file = tar.extractfile("./bot/.env")
        assert env_file is not None
        assert SECRET in env_file.read().decode()
        copy = tar.extractfile("./marzban/db.sqlite3")
        assert copy is not None
        Path(env["BACKUP_DIR"], "m.sqlite3").write_bytes(copy.read())
    with sqlite3.connect(Path(env["BACKUP_DIR"], "m.sqlite3")) as conn:
        assert conn.execute("SELECT name FROM users").fetchall() == [("client-1",)]


@needs_age
def test_the_copy_in_telegram_is_the_same_archive_and_nothing_is_left_behind(
    env: dict[str, str],
) -> None:
    run("backup_all.sh", env)
    (archive,) = archives(env)
    sent = Path(env["FAKE_SENT"])
    assert (sent / "sent.age").read_bytes() == archive.read_bytes()
    assert "Резервная копия" in (sent / "caption.txt").read_text(encoding="utf-8")
    mode = int((sent / "mode.txt").read_text(encoding="utf-8").strip(), 8)
    assert mode & 0o004  # the bot, uid 1000, is not the file's owner
    assert (Path(env["BACKUP_DIR"]) / ".last-sent").exists()
    work = Path(env["FAKE_WORKDIR"])
    assert not (work / "data" / ".nightly-backup.db").exists()
    assert not (work / "data" / ".outgoing-backup.age").exists()
    assert not list(Path(env["BACKUP_DIR"]).glob("*.partial"))


@needs_age
def test_only_the_newest_copies_are_kept(env: dict[str, str]) -> None:
    backups = Path(env["BACKUP_DIR"])
    backups.mkdir()
    for n in range(8):
        old = backups / f"backup-2026010{n}T000000Z.tar.gz.age"
        old.write_text("old", encoding="utf-8")
        os.utime(old, (1_700_000_000 + n, 1_700_000_000 + n))
    run("backup_all.sh", {**env, "KEEP_LOCAL": "3"})
    left = archives(env)
    assert len(left) == 3
    assert max(left, key=lambda p: p.stat().st_mtime).stat().st_size > 100


@needs_age
def test_a_missing_marzban_database_is_flagged_not_hidden(env: dict[str, str]) -> None:
    result = run("backup_all.sh", {**env, "MARZBAN_DB": "/nonexistent/db.sqlite3"})
    assert result.returncode == 0, result.stderr
    assert "warning: нет базы Marzban" in result.stdout
    caption = Path(env["FAKE_SENT"], "caption.txt").read_text(encoding="utf-8")
    assert "нет базы Marzban" in caption
    (archive,) = archives(env)
    with open_archive(env, archive) as tar:
        info = tar.extractfile("./INFO.txt")
        assert info is not None
        assert "WARNING: нет базы Marzban" in info.read().decode()


@needs_age
def test_without_telegram_the_copy_stays_local(env: dict[str, str]) -> None:
    result = run("backup_all.sh", {**env, "SEND_TELEGRAM": "0"})
    assert "telegram=skipped" in result.stdout
    assert not any("send_backup.py file" in c for c in calls(env))


@needs_age
def test_a_telegram_failure_keeps_the_backup_and_says_so(env: dict[str, str]) -> None:
    result = run("backup_all.sh", {**env, "FAKE_SEND_FAIL": "1"})
    assert result.returncode == 0
    assert "telegram=failed" in result.stdout
    assert "не удалось отправить" in result.stdout
    assert len(archives(env)) == 1
    assert not (Path(env["BACKUP_DIR"]) / ".last-sent").exists()


@needs_age
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"AGE_RECIPIENT": "AGE-SECRET-KEY-1ABC"}, "not an age public key"),
        ({"AGE_RECIPIENT": ""}, "not an age public key"),
        ({"FAKE_DB_FAIL": "1"}, "bot database could not be copied"),
        ({"FAKE_NO_CONTAINER": "1"}, "container node-rental-bot-1 not found"),
    ],
)
def test_a_failure_stops_loudly_and_tells_the_owner(
    env: dict[str, str], changes: dict[str, str], reason: str
) -> None:
    result = run("backup_all.sh", {**env, **changes})
    assert result.returncode == 1
    assert "BACKUP_FAILED" in result.stderr
    assert reason in result.stderr
    assert archives(env) == []
    assert not list(Path(env["BACKUP_DIR"]).glob("*.partial"))
    # The bot is asked to write to the owner (unless it is the container that is gone).
    if "FAKE_NO_CONTAINER" not in changes:
        texts = Path(env["FAKE_SENT"], "texts.log")
        assert "Резервная копия не сделана" in texts.read_text(encoding="utf-8")


# ------------------------------------------------------------------ the restore
@needs_age
def test_restore_opens_the_archive_into_a_new_folder_and_checks_it(
    env: dict[str, str], tmp_path: Path
) -> None:
    run("backup_all.sh", env)
    (archive,) = archives(env)
    dest = tmp_path / "restored"
    result = run("restore_backup.sh", env, str(archive), env["AGE_IDENTITY"], str(dest))
    assert result.returncode == 0, result.stderr
    assert "RESTORE_OK" in result.stdout
    assert (dest / "bot" / "bot.db").read_text(encoding="utf-8").strip() == (
        "BOT-DATABASE-CONTENT"
    )
    assert SECRET in (dest / "bot" / ".env").read_text(encoding="utf-8")
    assert "Nothing here" in result.stdout  # it only tells what to copy

    again = run("restore_backup.sh", env, str(archive), env["AGE_IDENTITY"], str(dest))
    assert again.returncode == 1
    assert "already exists" in again.stderr


def _break_age_and_offer_rage(env: dict[str, str], real_age: str) -> None:
    """The owner's phone: every Go program in Termux fails, rage is installed."""
    bin_dir = Path(env["PATH"].split(":")[0])
    (bin_dir / "age").write_text(
        '#!/usr/bin/env bash\necho "age $*" >> "$FAKE_LOG"\n'
        'echo "age: error: too many arguments" >&2\nexit 1\n',
        encoding="utf-8",
    )
    (bin_dir / "rage").write_text(
        f'#!/usr/bin/env bash\necho "rage $*" >> "$FAKE_LOG"\nexec {real_age} "$@"\n',
        encoding="utf-8",
    )
    for name in ("age", "rage"):
        (bin_dir / name).chmod(0o755)


@needs_age
def test_restore_uses_rage_when_age_cannot_run(
    env: dict[str, str], tmp_path: Path
) -> None:
    real_age = shutil.which("age")
    assert real_age is not None
    run("backup_all.sh", env)
    (archive,) = archives(env)
    _break_age_and_offer_rage(env, real_age)

    dest = tmp_path / "restored"
    result = run("restore_backup.sh", env, str(archive), env["AGE_IDENTITY"], str(dest))
    assert result.returncode == 0, result.stderr
    assert "RESTORE_OK" in result.stdout
    assert (dest / "bot" / "bot.db").read_text(encoding="utf-8").strip() == (
        "BOT-DATABASE-CONTENT"
    )
    log = calls(env)
    assert "age --version" in log  # asked first, and it failed
    assert any(c.startswith("rage -d -i ") for c in log)
    assert not any(c.startswith("age -d") for c in log)


@needs_age
def test_restore_says_what_to_install_when_neither_age_nor_rage_runs(
    env: dict[str, str], tmp_path: Path
) -> None:
    real_age = shutil.which("age")
    assert real_age is not None
    run("backup_all.sh", env)
    (archive,) = archives(env)
    _break_age_and_offer_rage(env, real_age)
    (Path(env["PATH"].split(":")[0]) / "rage").unlink()
    if shutil.which("rage", path=env["PATH"]):
        pytest.skip("a rage is installed on this machine")

    dest = tmp_path / "restored"
    result = run("restore_backup.sh", env, str(archive), env["AGE_IDENTITY"], str(dest))
    assert result.returncode == 1
    assert "pkg install rage" in result.stderr
    assert not dest.exists()


@needs_age
def test_restore_refuses_the_wrong_key_and_a_damaged_archive(
    env: dict[str, str], tmp_path: Path
) -> None:
    run("backup_all.sh", env)
    (archive,) = archives(env)
    _, stranger = make_keys(tmp_path / "bin")
    wrong = run(
        "restore_backup.sh", env, str(archive), str(stranger), str(tmp_path / "a")
    )
    assert wrong.returncode == 1
    assert "RESTORE_FAILED" in wrong.stderr
    assert not (tmp_path / "a").exists()

    damaged = tmp_path / "damaged.age"
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    damaged.write_bytes(bytes(data))
    broken = run(
        "restore_backup.sh", env, str(damaged), env["AGE_IDENTITY"], str(tmp_path / "b")
    )
    assert broken.returncode == 1
    assert not (tmp_path / "b").exists()


# ------------------------------------------------------------------- the installer
@needs_age
@needs_root
def test_the_installer_writes_the_timer_and_makes_the_first_backup(
    env: dict[str, str], tmp_path: Path
) -> None:
    etc = tmp_path / "etc"
    options = {**env, "ETC_DIR": str(etc)}
    result = run("install_backup.sh", options, env["AGE_RECIPIENT"])
    assert result.returncode == 0, result.stderr
    assert "BACKUP_INSTALL_OK" in result.stdout
    assert "BACKUP_OK" in result.stdout
    assert f"BACKUP_DIR={env['BACKUP_DIR']}" in (etc / "node-backup.env").read_text(
        encoding="utf-8"
    )
    assert len(archives(env)) == 1  # in the folder asked for, not in /root

    conf = (etc / "node-backup.env").read_text(encoding="utf-8")
    assert f"AGE_RECIPIENT={env['AGE_RECIPIENT']}" in conf
    assert oct((etc / "node-backup.env").stat().st_mode & 0o777) == "0o600"
    unit = etc / "systemd" / "system"
    service = (unit / "node-backup.service").read_text(encoding="utf-8")
    assert f"ExecStart=/bin/bash {ROOT}/scripts/backup_all.sh" in service
    timer = (unit / "node-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:20:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "systemctl enable --now node-backup.timer" in calls(env)


@needs_age
@needs_root
def test_the_installer_can_keep_every_copy_on_the_server(
    env: dict[str, str], tmp_path: Path
) -> None:
    etc = tmp_path / "etc"
    options = {**env, "ETC_DIR": str(etc)}
    result = run("install_backup.sh", options, env["AGE_RECIPIENT"], "--no-telegram")
    assert result.returncode == 0, result.stderr
    assert "BACKUP_INSTALL_OK" in result.stdout
    assert "telegram=skipped" in result.stdout  # the first copy is not sent either
    assert "No copy leaves this server" in result.stdout
    assert "SEND_TELEGRAM=0" in (etc / "node-backup.env").read_text(encoding="utf-8")
    assert not any("send_backup.py file" in c for c in calls(env))
    assert len(archives(env)) == 1

    again = run("install_backup.sh", options, env["AGE_RECIPIENT"])  # changed mind
    assert again.returncode == 0, again.stderr
    assert "SEND_TELEGRAM=1" in (etc / "node-backup.env").read_text(encoding="utf-8")


@needs_root
def test_the_installer_refuses_a_folder_that_is_not_a_plain_path(
    env: dict[str, str], tmp_path: Path
) -> None:
    # The folder goes into a file that bash sources: no $(...) may get through.
    options = {**env, "ETC_DIR": str(tmp_path / "etc"), "BACKUP_DIR": "/x/$(id)"}
    result = run("install_backup.sh", options, env["AGE_RECIPIENT"])
    assert result.returncode == 1
    assert "plain absolute path" in result.stderr
    assert not (tmp_path / "etc" / "node-backup.env").exists()


@needs_root
@pytest.mark.parametrize("extra", [["--telegram"], ["--no-telegram", "x"]])
def test_the_installer_rejects_options_it_does_not_know(
    env: dict[str, str], tmp_path: Path, extra: list[str]
) -> None:
    result = run(
        "install_backup.sh",
        {**env, "ETC_DIR": str(tmp_path / "etc")},
        env["AGE_RECIPIENT"],
        *extra,
    )
    assert result.returncode == 1
    assert "usage: install_backup.sh" in result.stderr
    assert not (tmp_path / "etc" / "node-backup.env").exists()


@needs_root
@pytest.mark.parametrize(
    "key",
    [
        "",
        "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ",
        "age1short",
    ],
)
def test_the_installer_refuses_anything_but_a_public_key(
    env: dict[str, str], tmp_path: Path, key: str
) -> None:
    result = run("install_backup.sh", {**env, "ETC_DIR": str(tmp_path / "etc")}, key)
    assert result.returncode == 1
    assert "PUBLIC key" in result.stderr
    assert not (tmp_path / "etc" / "node-backup.env").exists()


# ----------------------------------------------------------------- the doctor's line
def status(env: dict[str, str], tmp_path: Path, **extra: str) -> str:
    result = run("backup_status.sh", {**env, **extra})
    assert result.returncode == 0
    return result.stdout.strip()


def test_status_without_a_schedule_is_a_warning_with_the_fix(
    env: dict[str, str], tmp_path: Path
) -> None:
    line = status(env, tmp_path)
    assert line.startswith("WARN no scheduled backup")
    assert "install_backup.sh" in line


def test_status_follows_the_age_of_the_archive_and_of_the_copy_sent(
    env: dict[str, str], tmp_path: Path
) -> None:
    conf = tmp_path / "node-backup.env"
    conf.write_text(f"BACKUP_DIR={env['BACKUP_DIR']}\n", encoding="utf-8")
    env = {**env, "BACKUP_ENV": str(conf)}
    backups = Path(env["BACKUP_DIR"])
    backups.mkdir()
    assert status(env, tmp_path).startswith("WARN backup is set up but no archive")

    archive = backups / "backup-20260101T000000Z.tar.gz.age"
    archive.write_text("x", encoding="utf-8")
    assert "no copy reached Telegram" in status(env, tmp_path)

    (backups / ".last-sent").write_text("", encoding="utf-8")
    assert status(env, tmp_path).startswith("OK backup: the newest is 0 h old")

    old = time.time() - 3 * 86400
    os.utime(archive, (old, old))
    assert "72 h old" in status(env, tmp_path)

    os.utime(archive, None)
    stale = time.time() - 5 * 86400
    os.utime(backups / ".last-sent", (stale, stale))
    assert "no copy reached Telegram" in status(env, tmp_path)
    assert status(env, tmp_path, SEND_TELEGRAM="0").startswith("OK")


def test_the_doctor_shows_the_status_line_and_the_bot_image_has_the_sender() -> None:
    doctor = (SCRIPTS / "update_bot.sh").read_text(encoding="utf-8")
    assert "backup_status.sh" in doctor.split("--doctor", 3)[-1]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "scripts/send_backup.py ./scripts/send_backup.py" in dockerfile


# ------------------------------------------------------------- the sender itself
async def test_the_sender_delivers_a_file_with_its_caption(tmp_path: Path) -> None:
    file = tmp_path / "x.age"
    file.write_bytes(b"encrypted")
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    assert await send_backup.deliver(bot, 77, "file", "caption", str(file)) == 0
    args = bot.send_document.await_args
    assert args.args[0] == 77
    assert args.kwargs["caption"] == "caption"


async def test_the_sender_refuses_a_missing_or_huge_file_and_sends_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    assert await send_backup.deliver(bot, 77, "file", "", str(tmp_path / "none")) == 1
    big = tmp_path / "big.age"
    big.write_bytes(b"12345")
    monkeypatch.setattr(send_backup, "MAX_BYTES", 4)
    assert (
        await send_backup.deliver(bot, 77, "file", "", str(big)) == send_backup.TOO_BIG
    )
    bot.send_document.assert_not_awaited()
    assert await send_backup.deliver(bot, 77, "text", "hello", "") == 0
    bot.send_message.assert_awaited_once_with(77, "hello")


async def test_the_sender_needs_arguments(capsys: Any) -> None:
    assert await send_backup.main([]) == send_backup.USAGE
    assert await send_backup.main(["file"]) == send_backup.USAGE
    assert "send_backup.py file" in capsys.readouterr().out
